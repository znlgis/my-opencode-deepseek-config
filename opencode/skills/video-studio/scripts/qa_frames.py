# -*- coding: utf-8 -*-
"""qa_frames.py — 帧级 / 时间轴级深度质检。

只看容器、响度、黑帧、锐度的容器级质检看不出"画面内部"的问题：
一帧白闪、一段近黑空窗、音效 cue 与视觉事件没对上——这些只有逐帧解码才能发现。

本模块提供：
1. sample_frames：统一走 ffmpeg 一次流式解码成 160x90 灰度（单进程管道，不逐帧开进程）。
2. flicker_scan：单帧闪烁扫描（d = |F[t] - (F[t-1]+F[t+1])/2|，8x8 分块取最大均值，
   阈值 + 孤立性判据），返回时刻、强度、异常块坐标。
3. black_windows：连续近黑片段（"画面空窗"），片头/片尾刻意淡入淡出不计。
4. luma_curve / loudness_curve：亮度与响度时间曲线（响度是近似值，见文档）。
5. static_warnings（"_static_warnings"，报告里进 warnings）：画面长时间"完全没有运动"
   （卡帧 / 忘了加动画）。判据是"最活跃的 8x8 块在 0.5 s 里是否变了 > 1.5 级灰度"，
   连续 6 s 全部不动才告警（不用"全帧平均亮度 3 s 波动 < 0.002"这类判据：它对逐字
   入场这类局部动画不敏感，自带模板片会误报 4 段）。
6. sync_check：cues.json 的音频提示点与视觉事件配对，报 dt 超容差项。支持
   cue 自带 visual_at / visual_t 画面锚点（PIL 路线没有 window.getEvents() 也能自校验），
   并可用 require_sync（环境变量 QA_REQUIRE_SYNC=1 / CLI --require-sync）把
   "同步门禁没执行"从 warning 升级为 error。
7. report：一次扫描产出 work/qa/deep.json（CONTRACT §4.5）与一张 PIL 诊断 PNG
   （亮度曲线 + 响度曲线 + 闪烁点 + 空窗区 + 同步偏差，一眼看出问题位置）。

判据与阈值来自 ai-history-film（MIT）的实测经验：
- 开发日志第十四轮"单帧闪烁"：逐帧解码成 160x90、每帧减去前后两帧平均、分块统计，
  找"只亮一帧"的格子。实测真实异常 10–15，正常运动画面 ≤ 6.4，因此默认阈值 6.0。
- 第五轮"画面空窗"：逐帧亮度扫出 ≥ 1 s 近黑片段（保留片头、片尾等刻意黑）。
- video/qa_sync.js：画面视觉事件（闪白/震屏/冲击环）与音频 cue 对照。
- video/audio/inspect_mix.py：短时 RMS 响度曲线思路（matplotlib 改为 PIL 手绘）。
改编自 ai-history-film（MIT），算法已按本项目接口重写，未整目录拷贝。

用法：
    python qa_frames.py <film.mp4> [--cues work/cues.json] [--out work/qa/deep.json]
                        [--png work/qa/deep.png] [--expect-duration 389.0]
    python qa_frames.py --selftest

依赖：只允许 stdlib + numpy + Pillow；解码统一走 ffmpeg（filmkit.FFMPEG 或环境变量
FILMKIT_FFMPEG）。正常 import 不做重活、不打印。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                    # 同目录的 filmkit 提供 FFMPEG / ffprobe 解析
    import filmkit as _K
except Exception:                       # noqa: BLE001 —— 独立拷贝本文件时仍要能跑
    _K = None


# ---------------------------------------------------------------- 可调参数
# 采样分辨率：全片统一 160x90 灰度（与上游经验一致，单帧 14400 字节，解码极快）
SAMPLE_W = 160
SAMPLE_H = 90
# 探测不到帧率时的兜底值（本项目成片固定 30 fps）
FPS_FALLBACK = 30.0
# 音频解码统一到 48 kHz 单声道 f32le
AUDIO_SR = 48000

# 单帧闪烁判据（单位：0–255 灰度）。上游实测：正常运动画面 ≤ 6.4，真实异常 10–15。
FLICKER_THRESH = 6.0
FLICKER_BLOCK = 8       # 8x8 分块
FLICKER_ISO = 1.5       # 孤立性比值：当前帧异常度 >= 1.5 x 前后帧异常度（单帧事件约 2.0）
# "回到原位"闸门：|F[t+1]-F[t-1]| 必须小于 1.6 x 中间帧偏离量。
# 单帧闪烁 ≈ 0（前后几乎相同）；加速运动 ≈ 2 以上（前后差更多）→ 排除。
FLICKER_RETURN_RATIO = 1.6
# 符号一致度下限：块内像素"同向"变化的比例。真闪白/闪黑整块同向（0.75–1.0）；
# 文字入场 / 母题动画 / 镜头缓推是有增有减的纹理变化（实测 0.42）。
# 低于此值不判 error（降级 warning 供人复核）——否则零拷贝首片会被 G9 误杀。
FLICKER_AGREE_MIN = 0.60
# 与已登记视觉事件（闪白/震屏/冲击环）重合多久内视为"设计好的重击"，不计 error
FLICKER_EXPLAIN_WIN = 0.2
# 镜头切点白名单窗口（秒）：素材线是硬切，切点前后两帧结构不同，二阶差分必然尖峰。
# 这类切点是作者声明的剪辑结构（不是"单帧闪烁"）；降级为 warning 且留痕，不静默放过。
SHOT_WIN = 0.12
# 转化为"可解释设计事件"的 event.kind（小写匹配；cues.json §4.2 的 events 由页面导出）
VISUAL_EVENT_KINDS = {
    "flash", "shake", "ring", "rings", "shakes", "flash_white",
    "impact", "hit", "burst", "shock", "shockwave", "闪白", "震屏", "冲击环", "冲击",
}

# 画面空窗判据
BLACK_LUMA_MAX = 0.045  # 平均亮度低于此值算"近黑"（0–1 归一化，约 11/255）
BLACK_MIN_DUR = 0.8     # 连续多久才算空窗（秒）
HEAD_SKIP = 1.2         # 片头多少秒内不算（刻意淡入）
TAIL_SKIP = 1.2         # 片尾多少秒内不算（刻意淡出）

# 音画同步
SYNC_TOL = 0.06         # dt 容差（秒）
SYNC_SAME_WIN = 2.0     # 同 kind 配对的最大搜索窗（秒）
# 异 kind 的"就近配对"只用于**确认同步**（|dt| <= tol 才认可），没有独立的搜索窗；
# 这个常量保留给"events 每项带可选 cue 字段、允许跨 kind 显式配对"的下一版（CONTRACT §5.4 兼容位）。
SYNC_NEAR_WIN = 0.5

# 画面"长时间静止"警告（可能卡帧 / 忘了加动画）
#
# 不用"全帧平均亮度 3 s 滑动窗内波动 < 0.002"这类判据：它对**局部动画**天生不敏感，
# 逐字入场、数字滚动、卡片切换只占全屏几个百分点，平均亮度几乎不动。实测自带模板片
# （30.10 s）会因此报出 4 段假"亮度长时间无变化"（411/814 个窗口命中），而画面一直在动。
#
# 本判据看"画面里最活跃的 8x8 块，在 0.5 s 里变了多少级灰度"：
#   drift[t] = max over 8x8 blocks of mean |F[t] - F[t-lag]| * 255,  lag = 0.5 s * fps
# 物理含义：把画面切成 8x8 块（160x90 下共 20x11 块，单块占全屏 0.53%），取
# "0.5 秒内变化最大的那一块的平均变化量"（单位：灰度级 0–255）。
#   - 完全冻结：0（只有编码噪声，实测 ≤ 1.5）
#   - 慢速漂移/呼吸光/缓推镜头：数级灰度（实测合成 6 级/0.5s 的慢渐变仍被判"在动"）
#   - 逐字入场 / 数字滚动 / 粒子：几十级灰度
# 用 0.5 s 而不是逐帧比较，是为了不被 H.264 的单帧量化抖动打断：same 画面在静止段里
# 偶发 intra 重编码会让相邻帧差 1–4 级灰度（实测 hold_6.0 的 6 s 冻结里有 5 帧如此），
# 逐帧阈值在"不被噪声误判"和"不漏掉真冻结"之间没有余量；隔 0.5 s 看，抖动被摊平，
# 而真正的慢动画在 0.5 s 里的累计位移是实打实的。
#
# 阈值实测（160x90 解码，H.264 yuv420p，30 fps）：
#   - 纯冻结段（渲染器卡住）：drift 0–1.5
#   - 合成慢渐变（6 级/0.5s）：6.0
#   - 模板片第 2 段"静态卡片 + 字幕逐字入场"：窗口内最大 drift > 4.5（不会被误判）
#   - 逐字入场/粒子/场景切换：20–108
# 取 1.5：比冻结噪声上限高，又明显低于任何肉眼可见的动画。
FLAT_MIN_DUR = 6.0      # 静止持续多久才告警（秒）：与 SKILL.md「静止超过 6 秒观众就走」
                        # 和 references/quality-gates.md「连续 >6 s ≈ 静止」同一口径。
                        # 旧代码的 3.0 s 与这两处文档承诺不一致（文档说 6 s、代码报 3 s）。
FLAT_LAG_S = 0.5        # 判据时间尺度：比较"当前帧"与"0.5 s 前那一帧"的块级差异
FLAT_DRIFT_EPS = 1.5    # 0.5 s 内最活跃 8x8 块的平均变化 ≤ 此值（灰度 0–255）→ "没动"

# 报告图表尺寸（PIL 手绘，1600x820 在 1080p 屏上可直接看）
PLOT_W = 1600
PLOT_H = 830
PLOT_ML = 96
PLOT_MR = 28


# ---------------------------------------------------------------- ffmpeg / ffprobe
_FFMPEG_CACHE = ""
_FFPROBE_CACHE = ""


def _ffmpeg() -> str:
    """解析 ffmpeg 可执行文件：优先 FILMKIT_FFMPEG 环境变量，其次 filmkit.FFMPEG。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    env = os.environ.get("FILMKIT_FFMPEG")
    if env and os.path.exists(env):
        _FFMPEG_CACHE = env
        return _FFMPEG_CACHE
    if _K is not None:
        try:
            exe = getattr(_K, "FFMPEG", None)
            if exe:
                _FFMPEG_CACHE = str(exe)
                return _FFMPEG_CACHE
        except Exception:               # noqa: BLE001
            pass
    _FFMPEG_CACHE = shutil.which("ffmpeg") or "ffmpeg"
    return _FFMPEG_CACHE


def _ffprobe() -> str:
    """解析 ffprobe（ffmpeg 同目录；找不到时由 _probe 回退到 ffmpeg -i 解析）。"""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE:
        return _FFPROBE_CACHE
    if _K is not None:
        try:
            p = _K.ffprobe_path()
            if p and os.path.exists(p):
                _FFPROBE_CACHE = str(p)
                return _FFPROBE_CACHE
        except Exception:               # noqa: BLE001
            pass
    exe = _ffmpeg()
    base = os.path.basename(exe)
    cand = os.path.join(os.path.dirname(exe),
                        ("ffprobe.exe" if base.lower().endswith(".exe") else "ffprobe"))
    if os.path.exists(cand):
        _FFPROBE_CACHE = cand
        return cand
    _FFPROBE_CACHE = shutil.which("ffprobe") or cand
    return _FFPROBE_CACHE


def _parse_rate(txt) -> float | None:
    """把 ffprobe 的 "30000/1001" / "30" 解析成帧率。"""
    txt = str(txt or "").strip()
    if not txt:
        return None
    try:
        if "/" in txt:
            num, _, den = txt.partition("/")
            num, den = float(num), float(den or 0)
            if num > 0 and den > 0:
                return num / den
            return None
        v = float(txt)
        return v if v > 0 else None
    except Exception:                   # noqa: BLE001
        return None


def _probe(path) -> dict:
    """探测成片：帧率、时长、分辨率、是否有音轨。ffprobe 缺失时用 ffmpeg -i 回退。"""
    path = str(path)
    info = {"fps": FPS_FALLBACK, "duration": 0.0, "width": None, "height": None,
            "has_audio": False, "sample_rate": None, "duration_source": "none"}
    fps_parsed = False      # ffprobe 已经给出合法帧率？（拿它判要不要跑 ffmpeg -i 兜底）
    p = subprocess.run(
        [_ffprobe(), "-v", "error",
         "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,sample_rate:format=duration",
         "-of", "json", path],
        capture_output=True)
    data = None
    if p.returncode == 0 and p.stdout:
        try:
            data = json.loads(p.stdout.decode("utf-8", "replace"))
        except Exception:               # noqa: BLE001
            data = None
    if isinstance(data, dict):
        fmt = data.get("format") or {}
        try:
            dur = float(fmt.get("duration") or 0.0)
            if dur > 0:
                info["duration"] = dur
                info["duration_source"] = "format"
        except Exception:               # noqa: BLE001
            pass
        for st in data.get("streams") or []:
            ct = str(st.get("codec_type") or "")
            if ct == "video" and info["width"] is None:
                try:
                    info["width"] = int(st.get("width") or 0) or None
                    info["height"] = int(st.get("height") or 0) or None
                except Exception:       # noqa: BLE001
                    pass
                fps = _parse_rate(st.get("r_frame_rate"))
                if fps is None or fps <= 0:
                    fps = _parse_rate(st.get("avg_frame_rate"))
                if fps and 1.0 <= fps <= 240.0:
                    info["fps"] = float(fps)
                    fps_parsed = True
                if not info["duration"]:
                    try:
                        vd = float(st.get("duration") or 0.0)
                        if vd > 0:
                            info["duration"] = vd
                            info["duration_source"] = "stream"
                    except Exception:   # noqa: BLE001
                        pass
            elif ct == "audio":
                info["has_audio"] = True
                if info["sample_rate"] is None:
                    try:
                        info["sample_rate"] = int(st.get("sample_rate") or 0) or None
                    except Exception:   # noqa: BLE001
                        pass
    # 只在 ffprobe 真的没给全信息时才回退到 ffmpeg -i。
    # 旧条件写的是 info["fps"] == FPS_FALLBACK，于是**所有 30 fps 成片**都会白跑一次
    # ffmpeg -i（实测每片多一次进程 + 全文件解析），而 30fps 恰恰是本项目最常见情况。
    if info["width"] is None or not fps_parsed or not info["duration"]:
        txt = ""
        try:
            q = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path], capture_output=True)
            txt = (q.stderr or b"").decode("utf-8", "replace")
        except Exception:               # noqa: BLE001
            txt = ""
        if txt:
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
            if m and not info["duration"]:
                try:
                    info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                        + float(m.group(3)))
                    info["duration_source"] = "ffmpeg"
                except Exception:       # noqa: BLE001
                    pass
            if info["fps"] == FPS_FALLBACK:
                m = re.search(r"([\d.]+)\s+fps", txt)
                if m:
                    fps = _parse_rate(m.group(1))
                    if fps and 1.0 <= fps <= 240.0:
                        info["fps"] = float(fps)
            if "Audio:" in txt:
                info["has_audio"] = True
            if info["width"] is None:
                m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
                if m:
                    info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    if not math.isfinite(info["fps"]) or info["fps"] <= 0:
        info["fps"] = FPS_FALLBACK
    return info


# ---------------------------------------------------------------- 视频解码
def _iter_frames(path, w=SAMPLE_W, h=SAMPLE_H, from_s=0.0, to_s=None):
    """流式逐帧解码成 (h,w) float32（0–1）。一次 ffmpeg 进程读完，不逐帧开进程。"""
    w, h = int(w), int(h)
    if w <= 0 or h <= 0:
        raise ValueError("采样宽高必须为正：%dx%d" % (w, h))
    path = str(path)
    if not os.path.exists(path):
        raise ValueError("视频不存在：%s" % path)
    start = max(0.0, float(from_s or 0.0))
    cmd = [_ffmpeg(), "-v", "error", "-nostdin"]
    if start > 1e-6:
        cmd += ["-ss", "%.6f" % start]
    cmd += ["-i", path]
    if to_s is not None:
        dur = float(to_s) - start
        if dur <= 0:
            raise ValueError("to_s（%s）必须大于 from_s（%s）" % (to_s, from_s))
        cmd += ["-t", "%.6f" % dur]
    cmd += ["-vf", "scale=%d:%d:flags=area" % (w, h),
            "-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    frame_bytes = w * h
    errf = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
    except FileNotFoundError:
        errf.close()
        raise ValueError("找不到 ffmpeg（%s），请设置 FILMKIT_FFMPEG 或装 ffmpeg" % cmd[0]) from None
    rem = b""
    try:
        while True:
            chunk = proc.stdout.read(frame_bytes * 48)
            if not chunk:
                break
            data = rem + chunk
            k = len(data) // frame_bytes
            for i in range(k):
                fr = np.frombuffer(data, dtype=np.uint8, count=frame_bytes,
                                   offset=i * frame_bytes).reshape(h, w)
                yield fr.astype(np.float32) * (1.0 / 255.0)
            rem = data[k * frame_bytes:]
    finally:
        try:
            proc.stdout.close()
        except Exception:               # noqa: BLE001
            pass
        rc = proc.wait()
        errf.seek(0)
        err = errf.read().decode("utf-8", "replace")
        errf.close()
    if rc != 0:
        raise RuntimeError("ffmpeg 解码失败（%s）：%s" % (path, (err or "").strip()[-600:]))


def sample_frames(path, w=160, h=90, from_s=0.0, to_s=None) -> np.ndarray:
    """解码成 (n, h, w) float32 灰度 0..1（n = 实际解码帧数）。

    长片注意：389 s @30fps 约 11670 帧，float32 约 670 MB；只想要曲线/闪烁时
    请用 luma_curve / flicker_scan / black_windows（内部流式处理，不整片驻留）。
    """
    frames = list(_iter_frames(path, w, h, from_s, to_s))
    if not frames:
        return np.zeros((0, int(h), int(w)), dtype=np.float32)
    return np.stack(frames, axis=0)


def _block_max(diff, block, signed=None) -> tuple:
    """把 (h,w) 差异图按 block×block 分块取均值最大，返回 (最大均值, bbox, 该块中值, 符号一致度)。

    中值是复核线索：整块一起变亮（真单帧事件）时中值也高；只有小块元素在动时
    均值被少数像素拉高、中值仍接近 0（见 references/frame-qa.md 的 16.933 s 案例）。

    符号一致度（signed 传带符号的二阶差分时才算）：整块**同向**变亮/变暗
    = 真闪白/闪黑；文字逐字入场、母题动画、镜头缓推产生的只是**有增有减**的纹理变化，
    符号一致度低。实测（零拷贝首片）：t=5.867s 被误报的块 score 51.5 / median 44.5，
    但符号一致度只有 0.42；同一位置人为注入 +50 灰闪白后一致度 0.75。
    """
    h, w = diff.shape
    b = max(1, int(block))
    rows = np.arange(0, h, b, dtype=np.int64)
    cols = np.arange(0, w, b, dtype=np.int64)
    sums = np.add.reduceat(np.add.reduceat(diff.astype(np.float64), rows, axis=0), cols, axis=1)
    rc = np.diff(np.append(rows, h)).astype(np.float64)
    cc = np.diff(np.append(cols, w)).astype(np.float64)
    means = sums / np.outer(rc, cc)
    idx = int(np.argmax(means))
    r, c = divmod(idx, int(means.shape[1]))
    y0, x0 = int(rows[r]), int(cols[c])
    y1, x1 = min(h, y0 + int(rc[r])), min(w, x0 + int(cc[c]))
    bb = (x0, y0, x1, y1)
    blk = diff[y0:y1, x0:x1]
    med = float(np.median(blk)) if blk.size else 0.0
    agree = None
    if isinstance(signed, np.ndarray) and signed.shape == diff.shape:
        sblk = signed[y0:y1, x0:x1].astype(np.float64)
        if sblk.size:
            m = float(sblk.mean())
            # 与"块内平均方向"同向的像素占比：整块同向变亮 ≈ 1.0，纹理/运动互有增减 ≈ 0.5
            agree = float(np.mean(np.sign(sblk) == np.sign(m))) if m != 0 else 0.0
    return float(means[r, c]), bb, med, agree


def _block_peak(diff, block) -> float:
    """(h,w) 差异图按 block×block 分块取均值的最大值（只要数值，不算 bbox / 中值）。

    与 _block_max 同一套分块口径；静止判据每帧都要调，省掉 bbox / median 的开销。
    """
    h, w = diff.shape
    b = max(1, int(block))
    rows = np.arange(0, h, b, dtype=np.int64)
    cols = np.arange(0, w, b, dtype=np.int64)
    sums = np.add.reduceat(np.add.reduceat(diff.astype(np.float64), rows, axis=0), cols, axis=1)
    rc = np.diff(np.append(rows, h)).astype(np.float64)
    cc = np.diff(np.append(cols, w)).astype(np.float64)
    return float((sums / np.outer(rc, cc)).max())


def _scan_full(path, w=SAMPLE_W, h=SAMPLE_H, block=FLICKER_BLOCK, fps=None):
    """一次流式解码：返回 (means[n], scores[n], boxes[n-2], returns[n], drift[n])。

    scores[t]  = |F[t] - (F[t-1]+F[t+1])/2| 的块最大均值（0–255 单位）——"中间帧偏了多少"；
    returns[t] = |F[t+1] - F[t-1]| 的同一块均值——"前后两帧彼此差多少"；
    drift[t]   = |F[t] - F[t-lag]| 的块最大均值（lag = FLAT_LAG_S * fps）——"0.5 s 里
                 最活跃的 8x8 块变了多少"，静止判据用；t < lag 的帧为 NaN（不参与判定）。

    为什么要 scores+returns 两个：二阶差分**分不清**"单帧闪烁"和"加速运动"。
    真正只亮一帧的事件，前后两帧几乎一样（returns ≈ 0）而中间偏得很多；
    加速运动（逐字入场、缓动推近）三个二阶差分都大，returns 也大。
    只看 scores 会把"运动最快的那一帧"报成闪烁——这是本项目实测踩到的假阳性。

    scores[0] / scores[n-1] 无前后帧，为 NaN。boxes[k] 对应帧 t=k+1 的 (bbox, 块内中值)。
    """
    means, scores, boxes, returns = [], [], [], []
    use_fps = float(fps) if fps and math.isfinite(float(fps)) and float(fps) > 0 else FPS_FALLBACK
    lag = max(1, int(round(float(FLAT_LAG_S) * use_fps)))
    history = []                      # 最近 lag+1 帧（float32 160x90 ≈ 0.06 MB/帧）
    drift = []
    prev2 = prev1 = None
    for fr in _iter_frames(path, w, h):
        means.append(float(fr.mean()))
        drift.append(_block_peak(np.abs(fr - history[-lag]) * 255.0, block)
                     if len(history) >= lag else float("nan"))
        history.append(fr)
        if len(history) > lag:
            history.pop(0)
        if prev2 is not None:
            signed = (prev1 - (prev2 + fr) * 0.5) * 255.0
            diff = np.abs(signed)
            s, bb, med, agree = _block_max(diff, block, signed=signed)
            scores.append(float(s))
            boxes.append((bb, med, agree))
            # 同一个块上算"前后两帧彼此差多少"，用于区分闪烁与加速运动
            if bb:
                x0, y0, x1, y1 = bb
                returns.append(float(np.mean(np.abs(fr[y0:y1, x0:x1] - prev2[y0:y1, x0:x1])) * 255.0))
            else:
                returns.append(0.0)
        prev2, prev1 = prev1, fr
    n = len(means)
    sc = np.full(n, np.nan, dtype=np.float64)
    rt = np.full(n, np.nan, dtype=np.float64)
    for k, v in enumerate(scores):
        sc[k + 1] = v
        rt[k + 1] = returns[k] if k < len(returns) else np.nan
    dr = np.asarray(drift, dtype=np.float64)
    return np.asarray(means, dtype=np.float64), sc, boxes, rt, dr


def _scan_means(path, w=SAMPLE_W, h=SAMPLE_H) -> np.ndarray:
    """只流式取每帧平均亮度（空窗 / 亮度曲线用，不存整片）。"""
    out = []
    for fr in _iter_frames(path, w, h):
        out.append(float(fr.mean()))
    return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------- 单帧闪烁
# "高分候选被运动闸拦截"的记录（高到什么程度才值得提醒人去复核）
FLICKER_SUSPECT_SCORE = 10.0


def _flicker_from_scores(scores, boxes, fps, thresh, returns=None, suppressed=None) -> list[dict]:
    """从逐帧异常度数组里挑"只亮一帧"的孤立异常。

    判据（t 取中间帧，三道闸全过才记 error）：
      1. 幅值：score[t] >= thresh；
      2. 孤立：score[t] >= FLICKER_ISO * max(score[t-1], score[t+1])；
      3. 回到原位：|F[t+1]-F[t-1]| <= FLICKER_RETURN_RATIO * score[t]。

    第 2 条：单帧事件的分差只被前后帧各看到"一半"（比值 ≈ 2.0），连续运动/硬切 ≈ 1.0。
    全白单帧闪时前后帧分数也有 ~89，所以不能要求"邻居低于阈值"；反之，连续小元素运动
    在阈值附近波动时，偶尔会"两侧恰好低于阈值"造成假孤立——见 references/frame-qa.md
    的 16.933 s 案例（6.48 vs 5.66/3.52，比值只有 1.15，不是单帧闪烁）。

    第 3 条：区分"单帧闪烁"和"加速运动最快的那一帧"（详见 frame-qa.md §2.2）。

    ⚠ 第 3 条有一个已知盲区：快速运镜/大幅位移段落里，若前后两帧因**运动**产生的差异
    大于 1.6 × 闪烁幅度，真实的单帧闪会被这道闸放过。所以这里把"分数很高
    （>= FLICKER_SUSPECT_SCORE）却被第 3 条拒绝"的候选收进 `suppressed`，
    由 report() 转成 warning —— **让风险可见，而不是静默放过**。
    """
    out = []
    n = int(len(scores))
    if n < 3:
        return out
    thresh = float(thresh)
    for t in range(1, n - 1):
        s = float(scores[t])
        if not math.isfinite(s) or s < thresh:
            continue
        p, q = float(scores[t - 1]), float(scores[t + 1])
        if not (math.isfinite(p) and math.isfinite(q)):
            continue
        if s < FLICKER_ISO * max(p, q):
            continue
        box = boxes[t - 1] if 0 <= t - 1 < len(boxes) else ((0, 0, 0, 0), 0.0, None)
        bb, med = box[0], box[1]
        agree = box[2] if len(box) > 2 else None
        r = None
        if returns is not None and t < len(returns):
            r = float(returns[t])
            if math.isfinite(r) and r > FLICKER_RETURN_RATIO * s:
                if suppressed is not None and s >= FLICKER_SUSPECT_SCORE:
                    suppressed.append({
                        "t": round(t / float(fps), 3),
                        "score": round(s, 2),
                        "return": round(r, 2),
                        "ratio": round(s / max(p, q, 1e-9), 2),
                        "bbox": [int(v) for v in bb],
                        "median": round(float(med), 2),
                    })
                continue
        out.append({"t": round(t / float(fps), 3),
                    "score": round(s, 2),
                    "bbox": [int(v) for v in bb],
                    "ratio": round(s / max(p, q, 1e-9), 2),
                    "median": round(float(med), 2),
                    **({"agree": round(float(agree), 2)} if agree is not None else {}),
                    **({"return": round(r, 2)} if r is not None and math.isfinite(r) else {})})
    return out


def flicker_scan(path, *, thresh=FLICKER_THRESH, block=FLICKER_BLOCK) -> list[dict]:
    """单帧闪烁扫描。

    返回 [{"t","score","bbox","ratio","median"}]：
      t       秒（3 位小数）
      score   二阶差分块最大均值，0–255 灰度单位
      bbox    160x90 采样坐标 (x0, y0, x1, y1)
      ratio   孤立比值 score / max(前, 后)，>= FLICKER_ISO 才会入列
      median  异常块内差异的**中值**：真单帧闪烁整块一起变（中值≈score），
              小元素漂移/加速运动只有少数像素变（中值远小于 score），是人工复核的关键线索
    """
    fps = _probe(path)["fps"]
    means, scores, boxes, returns, _drift = _scan_full(path, SAMPLE_W, SAMPLE_H, block, fps)
    return _flicker_from_scores(scores, boxes, fps, thresh, returns=returns)


# ---------------------------------------------------------------- 画面空窗
def _runs(mask) -> list[tuple[int, int]]:
    """布尔数组中连续 True 的区间 [(start, end_exclusive)]。"""
    a = np.asarray(mask, dtype=bool)
    if a.size == 0:
        return []
    idx = np.flatnonzero(np.diff(np.concatenate(([0], a.astype(np.int8), [0]))))
    return [(int(idx[i]), int(idx[i + 1])) for i in range(0, len(idx) - 1, 2)]


def _black_from_means(means, fps, luma_max, min_dur, head, tail) -> list[dict]:
    """由逐帧平均亮度求近黑连续段（片头/片尾按秒裁剪，裁后仍 >= min_dur 才记）。"""
    means = np.asarray(means, dtype=np.float64)
    n = int(len(means))
    if n == 0:
        return []
    fps = float(fps) if fps and fps > 0 else FPS_FALLBACK
    total = n / fps
    eff_head, eff_tail = float(head), float(tail)
    if total <= eff_head + eff_tail + float(min_dur):
        # 短片（< 首尾保护 + 最短空窗）：按比例缩小首尾窗口，避免全部被裁掉
        eff_head = min(eff_head, total * 0.1)
        eff_tail = min(eff_tail, total * 0.1)
    out = []
    for a, b in _runs(means < float(luma_max)):
        t0, t1 = a / fps, b / fps
        t0c, t1c = max(t0, eff_head), min(t1, total - eff_tail)
        if t1c - t0c + 1e-9 >= float(min_dur):
            out.append({"t0": round(t0c, 3), "t1": round(t1c, 3)})
    return out


def black_windows(path, *, luma_max=BLACK_LUMA_MAX, min_dur=BLACK_MIN_DUR,
                  head=HEAD_SKIP, tail=TAIL_SKIP) -> list[dict]:
    """画面空窗：平均亮度 < luma_max 连续 >= min_dur 秒，片头 head / 片尾 tail 内的部分不算。"""
    means = _scan_means(path)
    fps = _probe(path)["fps"]
    return _black_from_means(means, fps, luma_max, min_dur, head, tail)


# ---------------------------------------------------------------- 亮度 / 响度曲线
def luma_curve(path, fps=None) -> list[dict]:
    """逐帧亮度曲线 [{"t","v"}]（v 为 160x90 采样平均亮度 0..1）。fps 缺省自动探测。"""
    use_fps = float(fps) if fps else float(_probe(path)["fps"])
    if not math.isfinite(use_fps) or use_fps <= 0:
        use_fps = FPS_FALLBACK
    means = _scan_means(path)
    return [{"t": round(i / use_fps, 3), "v": round(float(v), 5)}
            for i, v in enumerate(means)]


def loudness_curve(audio_path, win=0.1) -> list[dict]:
    """短时响度曲线 [{"t","lufs"}]。

    解码成 48 kHz 单声道 f32le，按 win 秒算 RMS -> dBFS，再减去 0.691 得到**近似 LUFS**；
    这是相对值：没有 BS.1770 的 K 加权与门限，低频偏重的音乐会偏低 1–3 LU。
    绝对响度是否合规以 filmkit.loudness()（ffmpeg ebur128）为准；本曲线用来看结构
    （哪段掉下去、重击比环境高多少、空窗处是否也静了）。
    """
    win = float(win)
    if win <= 0:
        raise ValueError("win 必须 > 0（当前 %s）" % win)
    n_win = max(1, int(round(win * AUDIO_SR)))
    p = subprocess.run(
        [_ffmpeg(), "-v", "error", "-nostdin", "-i", str(audio_path),
         "-vn", "-sn", "-dn", "-f", "f32le", "-ac", "1", "-ar", str(AUDIO_SR), "pipe:1"],
        capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise ValueError("音频解码失败（%s）：%s"
                         % (audio_path, (p.stderr or b"").decode("utf-8", "replace")[-300:]))
    x = np.frombuffer(p.stdout, dtype=np.float32)
    n = int(len(x) // n_win)
    if n <= 0:
        return []
    block = x[: n * n_win].astype(np.float64).reshape(n, n_win)
    rms = np.sqrt(np.mean(block * block, axis=1) + 1e-24)
    db = 20.0 * np.log10(np.maximum(rms, 1e-6))     # 下限 -120 dB
    lufs = db - 0.691                              # 近似 LUFS（见 docstring）
    return [{"t": round((i + 0.5) * win, 3), "lufs": round(float(v), 2)}
            for i, v in enumerate(lufs)]


# ---------------------------------------------------------------- 音画同步
def _event_kind(ev) -> str:
    return str(ev.get("kind") or ev.get("type") or "").strip().lower()


def _src_of(obj) -> str:
    """取 cue / event 的来源标记（web / pil / file / …）；没写返回空串。"""
    try:
        return str(obj.get("src") or "").strip().lower()
    except AttributeError:                      # noqa: BLE001 —— 坏条目当"没写"
        return ""


def _src_compatible(cue, ev) -> bool:
    """两个条目能不能做同步配对：只有**双方都明确标了来源且不同**时才认为时间基准不同。

    render.py 在 engine=web 时把三路 cue 合并（film.json / scenes.py / 页面），
    其中 scenes.py 的 cue 按 PIL 时间轴、页面事件按页面时间轴——跨基准配对会报假 FAIL
    （实测：cue ui_blip @7.725s 配 页面事件 @9.550s，dt=+1.825s）。
    没标 src 的条目（手写 events / 缺 src 的 cues.json）一律视作兼容，保持向后兼容。
    """
    cs, es = _src_of(cue), _src_of(ev)
    return not (cs and es and cs != es)


def _compatible_events(cue, events) -> list:
    return [e for e in (events or []) if isinstance(e, dict) and _src_compatible(cue, e)]


def _match_cue(cue, events, tol) -> tuple[dict | None, float | None, str]:
    """给一个 cue 找对应视觉事件。

    规则：
      1. 同 kind：kind == cue.type，搜索窗 SYNC_SAME_WIN（2 s）；
      2. 异 kind：只取时间最近者，且**只有 |dt| <= tol 时才认可**（用作"确实同步了"的佐证）。
         超过容差就不配对——跨 kind 的"最近事件"不是"预期事件"，据此判超差会制造假 FAIL。

    返回 (event, dt, mode)，mode ∈ {"same","near","none"}；dt = event_t - cue_t。

    ⚠ 门禁会不会失效取决于**命名约定**：只有 events.kind 与 cue.type 同名时第 1 条才生效。
    若页面只导出 flash/shake/ring 而 cue 是 impact/whoosh，两者永远配不上，
    第 2 条又只做确认 —— 结果是全部放行（静默通过）。约定见 references/frame-qa.md §2.5。
    """
    ct = float(cue.get("t") or 0.0)
    typ = str(cue.get("type") or "").strip().lower()
    # 只在**同时间基准**的事件里找（跨源配对会报假 FAIL，见 _src_compatible）
    events = _compatible_events(cue, events)
    same = [e for e in events if _event_kind(e) == typ]
    if same:
        ev = min(same, key=lambda e: abs(float(e.get("t") or 0.0) - ct))
        dt = float(ev.get("t") or 0.0) - ct
        if abs(dt) <= max(SYNC_SAME_WIN, float(tol) * 10.0):
            return ev, dt, "same"
    # 异 kind 的"就近配对"只用来**确认同步**，不用来**判定超差**：
    # 画面上的闪白/冲击环和音频里的 whoosh/sub_drop 本来就不是一一对应，
    # 拿"最近的异 kind 事件"当预期时刻会制造假 FAIL（实测：4.10 的 data_chirp
    # 被 3.70 的交界黑场事件配对，报出 -0.40s 超差）。
    # 所以：异 kind 只在 |dt| <= tol 时认可（作为佐证），超过容差就不配对、不判超差。
    if events:
        ev = min(events, key=lambda e: abs(float(e.get("t") or 0.0) - ct))
        dt = float(ev.get("t") or 0.0) - ct
        if abs(dt) <= float(tol):
            return ev, dt, "near"
    return None, None, "none"


def sync_check(cues, events, tol=SYNC_TOL) -> list[dict]:
    """cues（音频提示点）与 events（画面视觉事件）配对，返回 dt 超容差的列表。

    返回 [{"cue","cue_t","event_t","dt"}]，dt = event_t - cue_t（秒，3 位小数）。
    配对规则（按优先级）：
      0. **cue 自带视觉锚点**：cue 里写了数值 visual_at / visual_t 时，
         直接拿它当"这条音效对应的画面时刻"自校验，不需要 events。
         这条是给 PIL 路线补的口子——pil 引擎没有 window.getEvents()，events 恒空，
         没有这条口子 sync 检查在 PIL 路线**永远是空的**，但 SKILL.md §7 把"
         dt ≤ 60 ms"写成通用门禁（文档承诺、代码没做到）。
         用法：scenes.py 的 cues() 钩子里返回
             {"at": "l03+0.35", "type": "impact", "visual_at": "abs:12.42"}
         或在 film.json 的 sfx.cues 里直接写 visual_at（秒）。CueBus 会原样带进 cues.json。
      1. 同 kind：kind == cue.type，搜索窗 SYNC_SAME_WIN（2 s）；
      2. 异 kind：只取时间最近者，且**只有 |dt| <= tol 时才认可**（作为"确实同步了"的佐证）。
         超过容差就不配对——跨 kind 的"最近事件"不是"预期事件"，据此判超差会制造假 FAIL。
    配不上的 cue 视为"没有对应视觉事件"，不判超差（很多音效本来就没有画面事件）。
    ⚠ 命名约定见 references/frame-qa.md §2.5：events 与 cue 不同名时本检查会静默全过。
    ⚠ 来源（src）过滤：cue 与事件**双方都标了 src 且不同**时不配对——web 引擎
    三路合并时 PIL 时间轴的 cue 与页面事件基准不同，跨基准配对会报假 FAIL。跳过数量由
    _sync_coverage().src_skipped 计数并在 _load_cues 里转成 warning（不静默）。
    """
    out = []
    cues = [c for c in (cues or []) if isinstance(c, dict)]
    events = [e for e in (events or []) if isinstance(e, dict) and e.get("t") is not None]
    if not cues:
        return out
    for cue in sorted(cues, key=lambda c: float(c.get("t") or 0.0)):
        ct = float(cue.get("t") or 0.0)
        # 锚点可以来自 _load_cues 解析后的 _anchor_t，也可以直接读原始 visual_at/visual_t
        anchor = cue.get("_anchor_t")
        if anchor is None:
            anchor = _cue_anchor(cue)
        if anchor is not None:
            # cue 自带视觉锚点：直接自校验（不依赖 events / 命名约定）
            try:
                at = float(anchor)
            except Exception:           # noqa: BLE001
                at = None
            if at is not None:
                dt = at - ct
                if abs(dt) > float(tol) + 1e-9:
                    out.append({"cue": str(cue.get("type") or ""),
                                "cue_t": round(ct, 3),
                                "event_t": round(at, 3),
                                "dt": round(dt, 3)})
                continue
        if not events:
            continue
        ev, dt, _mode = _match_cue(cue, events, tol)
        if ev is None or dt is None:
            continue
        if abs(dt) > float(tol) + 1e-9:
            out.append({"cue": str(cue.get("type") or ""),
                        "cue_t": round(ct, 3),
                        "event_t": round(float(ev.get("t") or 0.0), 3),
                        "dt": round(float(dt), 3)})
    return out


# ---------------------------------------------------------------- 报告
# cue 自带的"画面锚点"字段名（都是可选、数值秒）：谁先出现用谁。
# 语义：这条音效在画面上对应的**意图时刻**，用于 PIL 路线在没有 events 时自校验。
CUE_ANCHOR_KEYS = ("visual_at", "visual_t")


def _cue_anchor(cue) -> float | None:
    """取 cue 自带的画面锚点（秒）；没有或不是有限数值时返回 None。"""
    for k in CUE_ANCHOR_KEYS:
        if k not in cue:
            continue
        v = cue.get(k)
        if isinstance(v, bool):
            continue
        try:
            f = float(v)
        except Exception:               # noqa: BLE001
            continue
        if math.isfinite(f):
            return f
    return None


def _sync_coverage(cues, events, tol=SYNC_TOL) -> dict:
    """统计"音画同步到底查了多少条"：返回 {"cues","anchors","same","judged","src_skipped"}。

    judged = **按命名约定真的判过 dt** 的 cue 数 = 自带视觉锚点的 cue + 同 kind 且落在
    SYNC_SAME_WIN 搜索窗内、src 同基准的 cue。这是防"静默全过"的量化指标：
    judged == 0 而 cues > 0 时，sync 为空没有意义。
    ⚠ 不能只数"存在同名 kind 的 cue"：同名事件若都离 cue 超过 SYNC_SAME_WIN 的搜索窗，
    sync_check 一条也不会判，judged 却会记成"查过了"——require_sync 的"没查"守卫
    就被这个虚高计数骗过去（实测 cues=[impact@0.5] / events=[impact@100]
    -> judged=1、errors=[]，门禁根本没判）。所以这里用与 sync_check 同一个 _match_cue 判。
    ⚠ 也**不**把"异 kind 就近确认"（mode="near"）算进 judged：它只在 |dt|<=tol 时认可，
    是运气不是约定；把它算成"判过"会让命名约定失效的场景显示成"已判 1 条"。
    另外记 judged_any（含异 kind 佐证）只作诊断，不进报告口径。
    src_skipped = 因为"cue 与事件时间基准不同"（src 不同）而无法参与对照的 cue 数：
    web 引擎三路合并（film.json/scenes.py 用 PIL 时间轴、页面事件用页面时间轴）时会有，
    这种情况不能算"同步失败"，但也不能静默跳过 —— 由 _load_cues 转成 warning 点名。
    """
    cues = [c for c in (cues or []) if isinstance(c, dict)]
    events = [e for e in (events or []) if isinstance(e, dict)]
    anchors = sum(1 for c in cues if _cue_anchor(c) is not None)
    same = 0
    judged = 0
    judged_any = 0
    src_skipped = 0
    for c in cues:
        if _cue_anchor(c) is not None:
            judged += 1
            judged_any += 1
            continue
        compat = _compatible_events(c, events)
        if not compat and events:
            src_skipped += 1
            continue
        typ = str(c.get("type") or "").strip().lower()
        if typ in {_event_kind(e) for e in compat}:
            same += 1
        matched = _match_cue(c, compat, tol)[1] is not None
        if matched:
            judged_any += 1
        # 只有"同名 kind 配对成功"才算按约定判过（锚点上面已单独计入）
        if matched and any(_event_kind(e) == typ for e in compat):
            judged += 1
    return {"cues": len(cues), "anchors": anchors, "same": same,
            "judged": judged, "judged_any": judged_any, "src_skipped": src_skipped}


def _load_shots(shots_json) -> list:
    """镜头切点（秒）。支持三种写法：
      ["s04","s05"] 或 [{"id":"s04"}, ...]  —— 从 timeline.json 的同目录 work/ 里查，
      或直接给秒数 [12.5, 33.1]。
    查不到就返回空表（**不报错**）：白名单是"少一次误报"的优化，不该让质检跑不起来。
    """
    if not shots_json:
        return []
    try:
        if isinstance(shots_json, (list, tuple)):
            data = list(shots_json)
        else:
            with open(str(shots_json), "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for item in data or []:
        if isinstance(item, (int, float)):
            out.append(float(item))
        elif isinstance(item, dict):
            for k in ("t", "start", "at"):
                if isinstance(item.get(k), (int, float)):
                    out.append(float(item[k])); break
        elif isinstance(item, str):
            # 段落 id：交给调用方传秒数更可靠，这里忽略（避免猜错时间轴）
            continue
    return sorted(set(round(x, 3) for x in out))


def _load_cues(cues_json):
    """读 cues.json（或直接给 dict）。返回 (cues, events, warnings, info)。

    info = {"anchors","same","judged","blockers"}：
      blockers 是"同步门禁没能真正执行"的原因（人话），require_sync=True 时会被升级成 error。
    cues 里可选的 visual_at / visual_t（数值秒）会被解析成内部 _anchor_t，
    供 sync_check 在**没有 events**（pil 路线）时自校验。
    """
    warns = []
    info = {"cues": 0, "anchors": 0, "same": 0, "judged": 0, "blockers": []}
    if cues_json is None:
        info["blockers"].append(
            "没有提供 cues.json：音画同步对照没有执行（传 --cues work/cues.json 才能检查）")
        return [], [], warns, info
    data = cues_json
    if not isinstance(data, dict):
        path = str(cues_json)
        if not os.path.exists(path):
            info["blockers"].append(
                "cues.json 不存在（%s），音画同步对照没有执行" % path)
            return [], [], ["cues.json 不存在（%s），跳过音画同步对照" % path], info
        try:
            with open(path, encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as e:          # noqa: BLE001
            info["blockers"].append("cues.json 解析失败（%s）" % e)
            return [], [], ["cues.json 解析失败（%s），跳过音画同步对照" % e], info
    if not isinstance(data, dict):
        info["blockers"].append("cues.json 结构不是对象")
        return [], [], ["cues.json 结构不是对象，跳过音画同步对照"], info
    cues = [c for c in (data.get("cues") or []) if isinstance(c, dict)]
    raw_events = [e for e in (data.get("events") or []) if isinstance(e, dict)]
    # 只认"带有限数值 t"的事件：sync_check 内部本来就这么过滤（e.get("t") is not None），
    # 但 _sync_coverage / 命名约定守卫必须用**过滤后**的列表 —— 否则 events 里只要有
    # {"kind": "impact"} 这种缺 t 的条目，judged 就会被算成 1，命名约定守卫也被骗过，
    # 结果是"门禁看起来在查、实际一条都没判"且不报警（实测：cues=1/events=[{kind:impact}]
    # -> coverage judged=1，report errors=[] warnings=[]）。
    events = []
    dropped_t = 0
    for e in raw_events:
        t = e.get("t")
        try:
            tv = float(t)
        except (TypeError, ValueError):
            dropped_t += 1
            continue
        if not math.isfinite(tv):
            dropped_t += 1
            continue
        ev = dict(e)
        ev["t"] = tv
        events.append(ev)
    bad_anchor = []
    for c in cues:
        for k in CUE_ANCHOR_KEYS:
            if k in c:
                a = _cue_anchor(c)
                if a is None:
                    bad_anchor.append("%s@%s=%r" % (c.get("type"), c.get("t"), c.get(k)))
                else:
                    c["_anchor_t"] = a
                break
    if dropped_t:
        warns.append("cues.json 有 %d 个 event 缺数值 t（秒），已忽略；"
                     "音画同步只看得到带时间的视觉事件" % dropped_t)
    cov = _sync_coverage(cues, events)
    info.update(cov)
    if cov.get("src_skipped"):
        msg = ("有 %d 条 cue 因时间基准不同未参与音画同步对照（cue 与事件的 src 不是同一来源）："
               "web 引擎会把 film.json / scenes.py / 页面 cue 三路合并，其中 scenes.py 的 cue 按 "
               "PIL 时间轴、页面事件按页面时间轴 —— 跨基准配对会报假 FAIL，所以跳过并在这里计数。"
               "要让这些 cue 也参与对照，给 cue 写数值 visual_at（见 references/frame-qa.md §2.5）。"
               % cov["src_skipped"])
        warns.append(msg)
        info.setdefault("notes", []).append(msg)
    if bad_anchor:
        warns.append("有 %d 条 cue 的 %s 不是数值秒，已忽略（只认数字，如 12.42）：%s"
                     % (len(bad_anchor), "/".join(CUE_ANCHOR_KEYS),
                        "、".join(bad_anchor[:4])))
    if not cues and events:
        warns.append("cues.json 没有 cues 字段，音画同步对照跳过")
        info["blockers"].append("cues.json 只有 events 没有 cues，同步门禁无从对照")
    elif cues and not events and cov["anchors"] > 0:
        warns.append("cues.json 没有 events 字段，改用 %d 条 cue 自带的 visual_at 锚点自校验"
                     "（pil 路线的替代方案，见 references/frame-qa.md §2.5）" % cov["anchors"])
    elif cues and not events:
        msg = ("cues.json 没有 events 字段，音画同步对照跳过"
               "（web 引擎用 window.getEvents() 导出；pil 路线请给 cue 加数值 visual_at，"
               "或按 §2.5 登记 events）")
        warns.append(msg)
        info["blockers"].append(msg)
    elif cues and events:
        # 门禁静默失效的最后两道保险，分别覆盖两种"零超差但一条没查"：
        #   ① 命名约定失效：cue.type 与任何事件的 kind 都不同名（cov["same"] == 0）；
        #   ② 同名事件全落在 SYNC_SAME_WIN 搜索窗之外，一条 dt 都算不出来（cov["judged"] == 0）。
        # 两者都会让 sync_check 返回空列表——看起来干净，其实一条都没在查。
        # 判据用 cov["judged"]（按约定真的配对成功）而不是 cov["same"]（只是同名）。
        cue_types = {str(c.get("type") or "").strip().lower() for c in cues}
        ev_kinds = {_event_kind(e) for e in events}
        if cov["same"] == 0 and cov["anchors"] == 0:
            # 命名约定失效：cue.type 与任何事件的 kind 都不同名。这时 sync_check 可能靠
            # 异 kind 的"就近确认"蒙到几条（dt 恰好在容差内），但**没有任何一条是照约定
            # 配出来的**——对齐了是巧合，错位了也不会报。所以这条与 judged 无关，永远报。
            msg = ("音画同步检查可能没生效：%d 个 cue 的 type 与 %d 个事件的 kind 没有任何可配对的同名项"
                   "（同 kind 配对要求 events.kind == cue 的配方名，且双方 src 需属同一时间基准）。"
                   "sync 为空不代表同步正确——请按 references/frame-qa.md §2.5 的命名约定登记事件，"
                   "或把某个 cue 故意改错 0.3 s 重跑做反证。" % (len(cue_types), len(ev_kinds)))
            warns.append(msg)
            info["blockers"].append(msg)
        elif cov["judged"] == 0 and cov.get("judged_any", 0) == 0:
            # 同名 kind 有，但一条 dt 都算不出来：事件全落在 SYNC_SAME_WIN 搜索窗之外，
            # 或全被 src 过滤掉了。sync_check 同样"零超差"通过——必须让人看见。
            msg = ("音画同步检查可能没生效：%d 条 cue 里没有一条能判定 dt"
                   "（同名事件都不在 %.1f s 搜索窗内，或 src 时间基准不同）。"
                   "sync 为空不代表同步正确——请核对 events 的时间基准与时刻，"
                   "或给 cue 写数值 visual_at。" % (len(cues), SYNC_SAME_WIN))
            warns.append(msg)
            info["blockers"].append(msg)
    else:
        # 「有 cues.json 但里面既没有 cues 也没有 events」：文件解析成功、对照却无从做起。
        # 不记 blocker 的话 require_sync 也拦不住：report() 里把"没查"升级成 error 的条件
        # 要求 cues 非空，而这里 cues 为空 —— 于是空 cues.json 会静默通过整道同步门禁。
        msg = ("cues.json 里既没有 cues 也没有 events：音画同步对照没有执行"
               "（文件是空对象、只写了 version 字段？按 §4.2 补 cues / events）")
        warns.append(msg)
        info["blockers"].append(msg)
    return cues, events, warns, info


def _visual_event_times(events) -> list[float]:
    """可解释单帧闪烁的已登记视觉事件时刻（闪白/震屏/冲击环）。"""
    out = []
    for ev in events or []:
        if _event_kind(ev) in VISUAL_EVENT_KINDS and ev.get("t") is not None:
            try:
                out.append(float(ev["t"]))
            except Exception:           # noqa: BLE001
                pass
    return out


def _static_segments(drift, fps, blacks=None) -> list[tuple[float, float]]:
    """画面长时间"完全没有运动"（卡帧 / 忘了加动画）-> [(t0, t1)] 秒区间。

    判据（不用"全帧平均亮度 3 s 波动 < 0.002"，理由见下）：
      对每个窗口起点 i，看 (i+lag, i+w] 这 w-lag 个 drift 值（drift[t] = "t 与 t-lag
      之间最活跃的 8x8 块平均变了多少级灰度"）是否都 <= FLAT_DRIFT_EPS。
    全部命中 → 这 6 秒里画面里任何一个 8x8 块在任意 0.5 s 内都没动过，人眼就是一张定格。

    为什么不用全帧平均亮度：逐字入场 / 数字滚动只占全屏几个百分点，
    平均亮度几乎不变（实测模板片误报 4 段），而块级差分能看见它们。
    为什么不用"逐像素完全相等"：H.264 每帧都有量化/去块噪声（实测静态段仍有
    最多 4 级灰度抖动），要求像素级相等会把真卡帧放过。
    为什么用"隔 0.5 s 比"而不是"相邻帧比"：编码器的单帧抖动会被 0.5 s 的跨度摊平，
    真动画在 0.5 s 里的累计变化反而更明显（见 FLAT_LAG_S / FLAT_DRIFT_EPS 注释的实测分布）。

    blacks 传 _black_from_means 的结果：与"画面空窗"重合的段不重复告警
    （空窗已经是 error，再报一条"静止"只是噪音）。
    """
    drift = np.asarray(drift, dtype=np.float64)
    n = int(len(drift))
    fps = float(fps) if fps and math.isfinite(float(fps)) and float(fps) > 0 else FPS_FALLBACK
    w = int(round(FLAT_MIN_DUR * fps))
    lag = max(1, int(round(float(FLAT_LAG_S) * fps)))
    if n < w or w < 2 or lag >= w - 1:
        return []
    quiet = np.isfinite(drift) & (drift <= float(FLAT_DRIFT_EPS))
    if not bool(quiet.any()):
        return []
    # 窗口 i 覆盖帧 [i, i+w)；判定用 drift[i+lag .. i+w)：这些帧的"0.5 s 前"也在窗口内
    core = np.lib.stride_tricks.sliding_window_view(quiet[lag:], w - lag)
    all_quiet = core.all(axis=1)
    runs = _runs(all_quiet)
    out = []
    for a, b in runs:
        t0 = a / fps
        t1 = (b - 1 + w) / fps
        if blacks:
            if any(not (t1 <= float(bw["t0"]) or t0 >= float(bw["t1"])) for bw in blacks):
                continue                        # 与近黑空窗重叠：已由 error 覆盖
        out.append((t0, t1))
        if len(out) >= 6:
            break
    return out


def _static_warnings(drift, fps, blacks=None) -> list[str]:
    """画面长时间"完全没有运动"（卡帧 / 忘了加动画）-> 中文警告文本。

    判据见 _static_segments：连续 6 s 内"最活跃的 8x8 块在任意 0.5 s 里的变化"都 < 1.5 级灰度。
    为什么不用全帧平均亮度：逐字入场 / 数字滚动只占全屏几个百分点，平均亮度几乎不变
    （实测自带模板片因此误报 4 段），而块级差分能看见它们。
    blacks 传 _black_from_means 的结果：与"画面空窗"重合的段不重复告警
    （空窗已经是 error，再报一条"静止"只是噪音）。
    """
    segs = _static_segments(drift, fps, blacks=blacks)
    out = []
    for t0, t1 in segs:
        out.append(
            "画面长时间静止（可能卡帧/忘了加动画）：%.2f–%.2fs（连续 %.1fs 内最活跃的 8x8 块"
            "在任意 %.1f s 里的变化都 < %.1f 级灰度；判据见 references/frame-qa.md §2.4b）"
            % (t0, t1, t1 - t0, FLAT_LAG_S, FLAT_DRIFT_EPS))
    return out


def _write_json(rep, out_json):
    out_json = str(out_json)
    d = os.path.dirname(os.path.abspath(out_json))
    if d:
        os.makedirs(d, exist_ok=True)
    txt = json.dumps(rep, ensure_ascii=False, indent=1, allow_nan=False)
    with open(out_json, "w", encoding="utf-8") as fp:
        fp.write(txt + "\n")


def report(film, *, cues_json=None, shots_json=None, expect_duration=None,
           out_json=None, out_png=None, require_sync=None) -> dict:
    """对成片做帧级深检，返回 CONTRACT §4.5 结构的 dict（并可选写 JSON / PNG）。

    一次流式解码同时得到：亮度曲线、单帧闪烁、画面空窗、静止段；音频轨另解一次得到响度曲线。
    cues_json 可以是路径或已载入的 dict；events 里闪白/震屏/冲击环时刻会用来排除
    "设计好的重击"（记 warning，不记 error）。

    shots_json：镜头切点列表（秒）。素材线是硬切剪辑，切点前后两帧结构不同，
      二阶差分必然产生孤立尖峰。传了它，切点 ±SHOT_WIN 内的候选降级为 warning
      并单独记录条数（报告字段 flicker_shot_cut），既不误报也不隐藏。

    require_sync：把"音画同步门禁没能执行"从 warning 升级为 error。
      默认 None = 读环境变量 QA_REQUIRE_SYNC（1/true/yes/on 时开启），便于不改进 render.py
      就整体开闸（render.py 的 st_qa 调的正是本函数）。
      背景：SKILL.md §7 把"音画同步 dt ≤ 60 ms"写成通用门禁，但 --engine pil 时 cues.json
      没有 events（只有 web 引擎导 getEvents），sync 恒为空却静默 PASS。
      两种修法配合使用：① cue 写 visual_at 锚点（pil 路线自校验）；② 开 require_sync 让
      "一条都没查"变成 FAIL 而不是静默通过。
    """
    film = str(film)
    if not os.path.exists(film):
        raise ValueError("成片不存在：%s" % film)
    meta = _probe(film)
    fps = float(meta["fps"]) if meta.get("fps") else FPS_FALLBACK
    if not math.isfinite(fps) or fps <= 0:
        fps = FPS_FALLBACK
    means, scores, boxes, returns, drift = _scan_full(film, SAMPLE_W, SAMPLE_H,
                                                    FLICKER_BLOCK, fps)
    n = int(len(means))
    duration = n / fps if n else float(meta.get("duration") or 0.0)

    luma = [{"t": round(i / fps, 3), "v": round(float(v), 5)} for i, v in enumerate(means)]
    suppressed: list = []
    flicker = _flicker_from_scores(scores, boxes, fps, FLICKER_THRESH,
                                   returns=returns, suppressed=suppressed)
    blacks = _black_from_means(means, fps, BLACK_LUMA_MAX, BLACK_MIN_DUR, HEAD_SKIP, TAIL_SKIP)

    if require_sync is None:
        require_sync = str(os.environ.get("QA_REQUIRE_SYNC") or "").strip().lower() in (
            "1", "true", "yes", "on", "y", "t")
    cues, events, warnings, sync_info = _load_cues(cues_json)
    sync = sync_check(cues, events, SYNC_TOL) if cues else []

    explained_times = _visual_event_times(events)
    for fl in flicker:
        fl["expected"] = any(abs(fl["t"] - te) <= FLICKER_EXPLAIN_WIN for te in explained_times)
    # 镜头切点（shots_json）：切点附近的孤立尖峰是剪辑结构，不是单帧闪烁。
    # 与 expected 分开记：报告里单独统计"因切点降级"的条数，避免和设计事件混淆。
    _shots = _load_shots(shots_json)
    for fl in flicker:
        fl["at_shot_cut"] = (not fl.get("expected")) and any(
            abs(fl["t"] - st) <= SHOT_WIN for st in _shots)

    errors = []
    textured = []
    cut_downgraded = []
    for fl in flicker:
        if fl.get("expected"):
            continue
        bb = fl.get("bbox") or [0, 0, 0, 0]
        msg = ("t=%.3fs score=%.1f（孤立比值 %.2f，块中值 %.2f，异常块 %d,%d–%d,%d，阈值 %.1f）"
               % (fl["t"], fl["score"], fl.get("ratio", 0.0), fl.get("median", 0.0),
                  bb[0], bb[1], bb[2], bb[3], FLICKER_THRESH))
        # 符号一致度 = 块内像素是否**同向**变亮/变暗。
        # 真闪白/闪黑整块同向（实测 0.75–1.0）；文字逐字入场、母题动画、镜头缓推
        # 是"有增有减"的纹理变化（实测 0.42）。不降级的后果是首片就 fail 在 G9
        # （零拷贝自测实测：t=5.867s 标题逐字入场被误判）。
        # 处理：低于阈值不静默放过，降级为 warning 并保留全部数值，交给人工复核。
        fl["sign_agree"] = fl.get("agree")
        if fl.get("at_shot_cut"):
            cut_downgraded.append(msg)
            continue
        if fl.get("agree") is not None and float(fl["agree"]) < FLICKER_AGREE_MIN:
            textured.append(msg + ("，符号一致度 %.2f < %.2f → 判为纹理/动画扰动"
                                   % (float(fl["agree"]), FLICKER_AGREE_MIN)))
            continue
        errors.append("单帧闪烁 " + msg)
    if cut_downgraded:
        warnings.append("已按镜头切点降级 %d 条孤立尖峰（--shots 声明的剪辑结构，非缺陷）：%s"
                        % (len(cut_downgraded), "；".join(cut_downgraded[:3])
                           + ("…" if len(cut_downgraded) > 3 else "")))
    for msg in textured:
        warnings.append("疑似非闪烁（块内像素有增有减，像是文字/母题动画）：" + msg
                        + "；若画面确有单帧闪白，调低 FLICKER_AGREE_MIN 或登记设计事件")
    explained = sum(1 for fl in flicker if fl.get("expected"))
    if explained:
        warnings.append("已排除 %d 处与已登记视觉事件（闪白/震屏/冲击环）重合的单帧闪烁；"
                        "见 flicker[*].expected" % explained)
    # 高分候选被"回到原位"闸判成运动 → 转 warning。这是本判据的**已知盲区**：
    # 快速运镜段落里真实的单帧闪可能被这道闸放过，必须让人看得见、能复核。
    for sp in suppressed:
        warnings.append(
            "高分异常被判为运动（建议人工复核）t=%.3fs score=%.1f return=%.1f "
            "（return 与 score 之比 %.2f > %.1f，判定为加速运动；若画面上确有单帧白闪，"
            "调小 FLICKER_RETURN_RATIO 或登记为设计事件）"
            % (sp["t"], sp["score"], sp["return"], sp["return"] / max(sp["score"], 1e-9),
               FLICKER_RETURN_RATIO))
    for w in blacks:
        errors.append("画面空窗 %.2f–%.2fs（持续 %.2fs 近黑，阈值 %.3f，片头/片尾 %.1fs 内不计）"
                      % (w["t0"], w["t1"], w["t1"] - w["t0"], BLACK_LUMA_MAX, HEAD_SKIP))
    for s in sync:
        errors.append("音画同步超差：cue '%s' @ %.3fs 与视觉事件 @ %.3fs（dt=%+.3fs，容差 ±%.3fs）"
                      % (s["cue"], s["cue_t"], s["event_t"], s["dt"], SYNC_TOL))
    # 音画同步门禁"没查"的处理：默认 warning（可见），require_sync 时升级为 error。
    blockers = list(sync_info.get("blockers") or [])
    if require_sync:
        for msg in blockers:
            errors.append("音画同步门禁未执行（require_sync=开，按 error 处理）：%s" % msg)
        # 同一句话已经在 _load_cues 里进了 warnings，升级为 error 后去掉，避免一条问题报两遍
        warnings = [w for w in warnings if w not in blockers]
    if (require_sync and cues and sync_info.get("judged", 0) <= 0
            and not any("音画同步门禁未执行" in e for e in errors)):
        errors.append(
            "音画同步门禁未执行（require_sync=开）：%d 条 cue 里没有一条能判定 dt"
            "（既没有 cue 自带的 visual_at 锚点，也没有同名 kind 的事件）。"
            "SKILL.md §7 承诺了 dt ≤ %.0f ms，这条门禁必须真的在查。"
            % (len(cues), SYNC_TOL * 1000.0))
    if expect_duration is not None:
        try:
            exp = float(expect_duration)
        except Exception:               # noqa: BLE001
            exp = None
        if exp is not None and not math.isfinite(exp):
            # nan 会让 abs(duration - nan) > 0.5 恒为 False —— 也就是"传了预期时长，校验静默失效"。
            # 这属于调用方写错，不当作通过：记 warning 说明这次没查，别让它假扮成 PASS。
            warnings.append("expect_duration 不是有限数字（%r），本次不做预期时长校验" % (expect_duration,))
            exp = None
        if exp is not None and abs(duration - exp) > 0.5:
            errors.append("时长 %.3fs 与预期 %.3fs 不符（差 %+.3fs，容差 ±0.5s）"
                          % (duration, exp, duration - exp))

    if meta.get("has_audio"):
        try:
            loud = loudness_curve(film, win=0.1)
        except Exception as e:          # noqa: BLE001
            loud = []
            warnings.append("响度曲线生成失败：%s" % e)
    else:
        loud = []
        warnings.append("成片没有音轨，响度曲线为空（帧级判据照常检查）")
    warnings.extend(_static_warnings(drift, fps, blacks=blacks))

    rep = {"flicker": flicker, "flicker_shot_cut": cut_downgraded,
           "black_windows": blacks, "luma": luma, "loudness": loud,
           "sync": sync, "errors": errors, "warnings": warnings,
           # sync_info：同步门禁到底查了多少（render.py:1172 一直在读 deep["sync_info"]["judged"]，
           # 缺了这个键 QC 报告的 G9 就永远显示"同步未判"）。
           "sync_info": dict(sync_info)}
    if out_json:
        _write_json(rep, out_json)
    if out_png:
        _draw_png(rep, film, fps, duration, events, out_png)
    return rep


# ---------------------------------------------------------------- PNG 诊断图
_FONT_CACHE = {}
_FONT_CANDIDATES = {
    True: [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
           "/System/Library/Fonts/PingFang.ttc"],
    False: [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc"],
}


def _font(size, bold=False):
    key = (int(size), bool(bold))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for path in _FONT_CANDIDATES[bool(bold)]:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, int(size), index=0)
                _FONT_CACHE[key] = f
                return f
            except Exception:           # noqa: BLE001
                continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _time_ticks(duration) -> list[float]:
    if duration <= 5:
        step = 0.5
    elif duration <= 15:
        step = 1.0
    elif duration <= 40:
        step = 2.0
    elif duration <= 90:
        step = 5.0
    elif duration <= 240:
        step = 10.0
    elif duration <= 600:
        step = 30.0
    else:
        step = 60.0
    out, t = [], 0.0
    while t <= duration + 1e-9:
        out.append(round(t, 3))
        t += step
    return out


def _draw_png(rep, film, fps, duration, events, out_png):
    """PIL 手绘诊断图：亮度曲线 + 响度曲线 + 闪烁点 / 空窗区 / 同步偏差（不用 matplotlib）。"""
    W, H = PLOT_W, PLOT_H
    x0, x1 = PLOT_ML, W - PLOT_MR
    dur = max(float(duration), 1e-6)
    img = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(img)
    f_title = _font(24, True)
    f_sub = _font(15)
    f_axis = _font(13)
    f_small = _font(12)
    f_row = _font(14)
    ink = (32, 35, 42)
    gray = (110, 114, 124)
    grid = (226, 228, 234)
    axis = (90, 94, 102)
    c_luma = (31, 111, 208)
    c_loud = (219, 120, 35)
    c_err = (214, 40, 40)
    c_warn = (236, 146, 32)
    c_sync = (128, 62, 190)
    c_event = (40, 150, 90)

    luma_top, luma_bot = 84, 292
    loud_top, loud_bot = 334, 528
    ev_top, ev_bot = 570, 748

    def X(t):
        return x0 + max(0.0, min(1.0, float(t) / dur)) * (x1 - x0)

    def y_luma(v):
        v = max(0.0, min(1.0, float(v)))
        return luma_bot - v * (luma_bot - luma_top)

    ticks = _time_ticks(dur)

    # ---- 标题 ----
    base = os.path.basename(film)
    d.text((PLOT_ML, 20), "qa_frames 深度质检 — %s" % base, fill=ink, font=f_title)
    n_err = len(rep.get("errors") or [])
    explained = sum(1 for fl in (rep.get("flicker") or []) if fl.get("expected"))
    sub = ("时长 %.2f s · %.2f fps · 采样 160×90 · 闪烁 %d（设计事件 %d） · 空窗 %d · "
           "同步超差 %d"
           % (duration, fps, len(rep.get("flicker") or []), explained,
              len(rep.get("black_windows") or []), len(rep.get("sync") or [])))
    d.text((PLOT_ML, 52), sub, fill=gray, font=f_sub)
    badge = "PASS" if n_err == 0 else "FAIL ×%d" % n_err
    bw = d.textlength(badge, font=f_sub) + 24
    d.rounded_rectangle([W - PLOT_MR - bw, 20, W - PLOT_MR, 46], radius=10,
                        fill=(38, 150, 90) if n_err == 0 else (206, 56, 56))
    d.text((W - PLOT_MR - bw + 12, 33), badge, fill=(255, 255, 255), font=f_sub, anchor="lm")

    # ---- 时间网格（各面板垂直对齐，方便看同一时刻）----
    for t in ticks:
        x = X(t)
        for top, bot in ((luma_top, luma_bot), (loud_top, loud_bot), (ev_top, ev_bot)):
            d.line([x, top, x, bot], fill=grid, width=1)
    d.line([x0, ev_bot, x1, ev_bot], fill=axis, width=1)
    for t in ticks:
        x = X(t)
        d.line([x, ev_bot, x, ev_bot + 5], fill=axis, width=1)
        lab = ("%g" % t) if t < 60 else ("%d" % int(t))
        d.text((x, ev_bot + 6), lab, fill=axis, font=f_axis, anchor="ma")
    d.text((x1, ev_bot + 22), "时间（秒）", fill=gray, font=f_axis, anchor="ra")

    # ---- 空窗阴影（先画，曲线压在上面）----
    blacks = rep.get("black_windows") or []
    for w in blacks:
        xa, xb = X(w["t0"]), X(w["t1"])
        if xb - xa < 1:
            xb = xa + 1
        for top, bot, tint in ((luma_top, luma_bot, (255, 216, 216)),
                               (loud_top, loud_bot, (255, 226, 222)),
                               (ev_top, ev_bot, (255, 232, 230))):
            d.rectangle([xa, top, xb, bot], fill=tint)
    if blacks:
        for w in blacks:
            d.text((min(X(w["t0"]) + 4, x1 - 90), luma_top + 4),
                   "空窗 %.1fs" % (w["t1"] - w["t0"]), fill=c_err, font=f_small)

    # ---- 面板 1：亮度曲线 ----
    d.rectangle([x0, luma_top, x1, luma_bot], outline=(205, 208, 214), width=1)
    for k in range(5):
        v = k / 4.0
        y = y_luma(v)
        d.line([x0, y, x1, y], fill=grid, width=1)
        d.text((x0 - 8, y), "%.2f" % v, fill=axis, font=f_axis, anchor="rm")
    d.text((x0 + 6, luma_top + 2), "亮度曲线（160×90 灰度均值，0–1）", fill=gray, font=f_small)
    pts = [(X(p["t"]), y_luma(p["v"])) for p in (rep.get("luma") or [])]
    if len(pts) >= 2:
        d.line(pts, fill=c_luma, width=2, joint="curve")

    # ---- 面板 2：响度曲线 ----
    d.rectangle([x0, loud_top, x1, loud_bot], outline=(205, 208, 214), width=1)
    loud = rep.get("loudness") or []
    if loud:
        vals = [float(p["lufs"]) for p in loud if math.isfinite(float(p["lufs"]))]
        lo = max(-90.0, (min(vals) - 3.0) if vals else -60.0)
        hi = min(0.0, (max(vals) + 3.0) if vals else 0.0)
        if hi - lo < 12.0:
            hi = lo + 12.0
        if hi > 0:
            hi = 0.0
            lo = min(lo, hi - 12.0)

        def y_loud(v):
            v = max(lo, min(hi, float(v)))
            return loud_bot - (v - lo) / (hi - lo) * (loud_bot - loud_top)

        g = math.ceil(lo / 10.0) * 10.0
        while g <= hi + 1e-9:
            y = y_loud(g)
            d.line([x0, y, x1, y], fill=grid, width=1)
            d.text((x0 - 8, y), "%d" % int(round(g)), fill=axis, font=f_axis, anchor="rm")
            g += 10.0
        lpts = [(X(p["t"]), y_loud(p["lufs"])) for p in loud]
        if len(lpts) >= 2:
            d.line(lpts, fill=c_loud, width=2, joint="curve")
        d.text((x0 + 6, loud_top + 2), "响度曲线（RMS→dBFS→近似 LUFS，0.1 s 窗）",
               fill=gray, font=f_small)
    else:
        d.text(((x0 + x1) / 2, (loud_top + loud_bot) / 2), "无音轨（跳过响度曲线）",
               fill=gray, font=f_sub, anchor="mm")

    # ---- 闪烁竖线（亮度/响度面板对齐）----
    flickers = rep.get("flicker") or []
    for fl in flickers:
        x = X(fl["t"])
        col = c_warn if fl.get("expected") else c_err
        for top, bot in ((luma_top, luma_bot), (loud_top, loud_bot)):
            d.line([x, top, x, bot], fill=col, width=2)
        d.polygon([(x - 5, luma_top), (x + 5, luma_top), (x, luma_top + 9)], fill=col)
    if len(flickers) <= 10:
        for fl in flickers:
            d.text((X(fl["t"]) + 3, luma_top + 11), "%.2fs" % fl["t"],
                   fill=c_warn if fl.get("expected") else c_err, font=f_small)

    # ---- 面板 3：事件带 ----
    d.rectangle([x0, ev_top, x1, ev_bot], outline=(205, 208, 214), width=1)
    rows = [("单帧闪烁", c_err), ("画面空窗", c_err), ("同步超差", c_sync)]
    if events:
        rows.append(("设计事件", c_event))
    row_h = (ev_bot - ev_top) / float(len(rows))
    for i, (name, _col) in enumerate(rows):
        cy = ev_top + (i + 0.5) * row_h
        d.line([x0, cy, x1, cy], fill=(236, 238, 242), width=1)
        d.text((x0 - 8, cy), name, fill=axis, font=f_row, anchor="rm")
    # 闪烁
    cy = ev_top + 0.5 * row_h
    for fl in flickers:
        x = X(fl["t"])
        col = c_warn if fl.get("expected") else c_err
        d.line([x, cy - row_h * 0.32, x, cy + row_h * 0.32], fill=col, width=3)
    # 空窗
    cy = ev_top + 1.5 * row_h
    for w in blacks:
        d.rectangle([X(w["t0"]), cy - row_h * 0.22, X(w["t1"]), cy + row_h * 0.22],
                    fill=(235, 110, 110))
    # 同步
    cy = ev_top + 2.5 * row_h
    for s in (rep.get("sync") or []):
        x = X(s["cue_t"])
        d.line([x, cy - row_h * 0.32, x, cy + row_h * 0.32], fill=c_sync, width=3)
        if len(rep.get("sync") or []) <= 6:
            d.text((x + 4, cy - row_h * 0.42), "dt=%+.2fs" % s["dt"],
                   fill=c_sync, font=f_small)
    # 设计视觉事件
    if events:
        cy = ev_top + 3.5 * row_h
        for ev in events:
            if ev.get("t") is None:
                continue
            try:
                x = X(float(ev["t"]))
            except Exception:           # noqa: BLE001
                continue
            d.line([x, cy - row_h * 0.28, x, cy + row_h * 0.28], fill=c_event, width=2)
            if len(events) <= 14:
                d.text((x + 2, cy - row_h * 0.40), _event_kind(ev)[:8],
                       fill=c_event, font=f_small)

    # ---- 页脚：判据摘要 ----
    foot1 = ("判据：单帧闪烁 = 8x8 块 |F[t]-(F[t-1]+F[t+1])/2| 最大均值 >= %.1f 且孤立；"
             "空窗 = 亮度 < %.3f 连续 ≥ %.1f s（片头/片尾 %.1f s 内不计）"
             % (FLICKER_THRESH, BLACK_LUMA_MAX, BLACK_MIN_DUR, HEAD_SKIP))
    foot2 = ("音画同步 = cue 与视觉事件 |dt| > %.2f s；闪烁中橙色与已登记闪白/震屏/冲击环"
             "重合（设计重击，不计 error）" % SYNC_TOL)
    d.text((PLOT_ML, 788), foot1, fill=gray, font=f_small)
    d.text((PLOT_ML, 808), foot2, fill=gray, font=f_small)
    d.text((W - PLOT_MR, 808), "qa_frames", fill=(160, 163, 172), font=f_small,
           anchor="ra")

    out_png = str(out_png)
    d2 = os.path.dirname(os.path.abspath(out_png))
    if d2:
        os.makedirs(d2, exist_ok=True)
    img.save(out_png, format="PNG", optimize=True)
    return out_png


# ---------------------------------------------------------------- 命令行输出
def _utf8_stdout():
    """CLI / selftest 里把 stdout/stderr 设成 UTF-8，避免 Windows 控制台编码炸中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:               # noqa: BLE001
            pass


def _print_summary(rep, film, out_json=None, out_png=None):
    fl = rep.get("flicker") or []
    explained = sum(1 for f in fl if f.get("expected"))
    print("== qa_frames 深度质检")
    print("   成片:", film)
    n_sup = sum(1 for w in (rep.get("warnings") or []) if "高分异常被判为运动" in str(w))
    n_static = sum(1 for w in (rep.get("warnings") or []) if "画面长时间静止" in str(w))
    si = rep.get("sync_info") or {}
    n_src = int(si.get("src_skipped") or 0)
    print("   闪烁 %d（其中与已登记视觉事件重合 %d） / 空窗 %d / 同步超差 %d%s%s%s"
          % (len(fl), explained, len(rep.get("black_windows") or []),
             len(rep.get("sync") or []),
             ("  / 跨基准未对照 %d" % n_src) if n_src else "",
             ("  / 静止段 %d" % n_static) if n_static else "",
             ("  / 高分异常待复核 %d" % n_sup) if n_sup else ""))
    print("   亮度采样 %d 点 / 响度采样 %d 点"
          % (len(rep.get("luma") or []), len(rep.get("loudness") or [])))
    for e in rep.get("errors") or []:
        print("   [FAIL]", e)
    for w in rep.get("warnings") or []:
        print("   [WARN]", w)
    if out_json:
        print("   报告 JSON:", os.path.abspath(str(out_json)))
    if out_png:
        print("   报告图表:", os.path.abspath(str(out_png)))
    n_err = len(rep.get("errors") or [])
    print("   结论:", ("FAIL（%d 个 error）" % n_err) if n_err else "PASS")


# ---------------------------------------------------------------- 自检
def _encode_rawvideo(out_mp4, frames, fps, audio_path=None):
    """把 (n,h,w) uint8 灰度帧编码成 mp4（rawvideo 管道 + libx264；可选 AAC 音轨）。"""
    frames = np.asarray(frames, dtype=np.uint8)
    if frames.ndim != 3:
        raise ValueError("frames 必须是 (n,h,w) 灰度数组")
    n, h, w = frames.shape
    cmd = [_ffmpeg(), "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "gray", "-s", "%dx%d" % (w, h),
           "-r", "%.6f" % float(fps), "-i", "pipe:0"]
    if audio_path:
        cmd += ["-i", str(audio_path)]
    cmd += ["-map", "0:v:0"]
    if audio_path:
        cmd += ["-map", "1:a:0"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio_path:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += [str(out_mp4)]
    p = subprocess.run(cmd, input=frames.tobytes(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("测试视频编码失败：%s"
                           % (p.stderr or b"").decode("utf-8", "replace")[-400:])
    return str(out_mp4)


def _make_test_audio(path, duration=8.0):
    """测试音轨：440 Hz 正弦（0–1.5 s、2.5–末尾），1.5–2.5 s 数字静音。"""
    n = int(round(float(duration) * AUDIO_SR))
    t = np.arange(n, dtype=np.float64) / AUDIO_SR
    x = 0.1 * np.sin(2.0 * math.pi * 440.0 * t)
    silent = (t >= 1.5) & (t < 2.5)
    x[silent] = 0.0
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(AUDIO_SR)
        wf.writeframes(pcm.tobytes())
    return str(path)


def _motion_frames(duration, fps, w, h, *, intro_black=0.0, black_win=None,
                   flash_at=None, drift_at=None, drift_win=None):
    """合成测试画面：平滑运动的条纹/渐变（正常运动，闪烁分数应远低于阈值）。

    intro_black：片头多少秒纯黑（验证片头不误报）；
    black_win：注入的 (t0,t1) 近黑空窗；
    flash_at：注入的单帧全白闪时刻；
    drift_win/drift_at：把 drift_win 秒区间内的画面**冻结**成同一张图（drift_at 是冻结起点），
       用来造"真的卡帧"——不是黑屏、不是静帧设计，而是渲染器把同一帧重复输出。
    """
    n = int(round(float(duration) * float(fps)))
    yy, xx = np.mgrid[0:int(h), 0:int(w)].astype(np.float32)
    out = np.zeros((n, int(h), int(w)), dtype=np.uint8)
    frozen = None
    for i in range(n):
        t = i / float(fps)
        fr = np.zeros((int(h), int(w)), dtype=np.float32)
        if t < float(intro_black):
            pass
        elif black_win is not None and float(black_win[0]) <= t < float(black_win[1]):
            pass
        else:
            phase = t * 1.7
            bars = 0.5 + 0.5 * np.sin(xx * (2.0 * math.pi / 70.0) + phase)
            grad = 0.55 + 0.45 * np.sin(yy * (2.0 * math.pi / int(h)) + phase * 0.6)
            fr = (0.22 + 0.24 * bars * grad).astype(np.float32)
        if flash_at is not None and abs(t - float(flash_at)) < 0.5 / float(fps):
            fr = np.ones((int(h), int(w)), dtype=np.float32)
        if drift_at is not None and drift_win is not None:
            if float(drift_at) <= t < float(drift_at) + float(drift_win):
                if frozen is None:
                    # 冻结成"上一帧那张图"：渲染器卡住时，第 3.000 s 输出的就是第 2.967 s
                    # 已经画好的那张图，所以 3.000 s 这一帧与前一帧是**逐字节相同**的。
                    # 这样 drift_win=6.0 恰好产生 180 个"与前一帧相同"的帧 = 整 6.0 s。
                    frozen = out[i - 1].copy() if i > 0 else np.clip(
                        fr * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
                out[i] = frozen
                continue
            frozen = None
        out[i] = np.clip(fr * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    return out


def selftest() -> int:
    """合成语料自检：不联网、≤ 60 s；PASS/FAIL 逐条打印，失败返回 1。"""
    import time
    _utf8_stdout()
    t_start = time.perf_counter()
    print("== qa_frames 自检（合成语料，不联网）")
    print("   ffmpeg:", _ffmpeg())
    tmp = tempfile.mkdtemp(prefix="qa_frames_selftest_")
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:          # noqa: BLE001
            ok, detail = False, "异常：%s" % e
        results.append((name, bool(ok), detail))
        print("   [%s] %s  %s" % ("PASS" if ok else "FAIL", name, detail))

    try:
        # ---- 合成语料 ----
        clean_mp4 = os.path.join(tmp, "clean.mp4")
        bad_mp4 = os.path.join(tmp, "injected.mp4")
        wav = os.path.join(tmp, "tone.wav")
        _make_test_audio(wav, duration=8.0)
        _encode_rawvideo(clean_mp4, _motion_frames(4.0, 30, 320, 180), 30)
        _encode_rawvideo(bad_mp4,
                         _motion_frames(8.0, 30, 320, 180, intro_black=0.8,
                                        black_win=(2.0, 3.5), flash_at=5.5),
                         30, audio_path=wav)
        cues_plain = os.path.join(tmp, "cues_plain.json")
        cues_flash = os.path.join(tmp, "cues_flash.json")
        base_cues = {"version": 1, "duration": 8.0, "fps": 30,
                     "cues": [{"t": 2.0, "type": "impact"},
                              {"t": 5.5, "type": "whoosh"},
                              {"t": 7.0, "type": "ui_blip"}],
                     "events": [{"t": 2.05, "kind": "impact"},
                                {"t": 5.9, "kind": "whoosh"},
                                {"t": 7.0, "kind": "ui_blip"}]}
        with open(cues_plain, "w", encoding="utf-8") as fp:
            json.dump(base_cues, fp, ensure_ascii=False)
        flash_cues = json.loads(json.dumps(base_cues))
        flash_cues["events"].append({"t": 5.5, "kind": "flash", "a": 0.8})
        with open(cues_flash, "w", encoding="utf-8") as fp:
            json.dump(flash_cues, fp, ensure_ascii=False)

        # ---- 逐条检查 ----
        def c_ffmpeg():
            p = subprocess.run([_ffmpeg(), "-version"], capture_output=True)
            if p.returncode != 0:
                return False, "ffmpeg -version 失败"
            return True, "可用"

        def c_sample():
            a = sample_frames(bad_mp4)
            if a.dtype != np.float32 or a.ndim != 3:
                return False, "dtype=%s ndim=%d" % (a.dtype, a.ndim)
            n, hh, ww = a.shape
            if (hh, ww) != (90, 160):
                return False, "形状 %s" % (a.shape,)
            if not (abs(n - 240) <= 2):
                return False, "帧数 %d（预期 240）" % n
            if float(a.min()) < -1e-6 or float(a.max()) > 1.0 + 1e-6:
                return False, "取值范围 [%.3f, %.3f]" % (float(a.min()), float(a.max()))
            return True, "shape=%s，范围 [%.2f, %.2f]" % (a.shape, float(a.min()), float(a.max()))

        def c_window():
            a = sample_frames(bad_mp4, from_s=2.1, to_s=3.4)
            if a.size == 0:
                return False, "时间窗内没有帧"
            if float(a.mean()) > 0.01:
                return False, "时间窗平均亮度 %.4f（应接近 0）" % float(a.mean())
            return True, "%d 帧，平均亮度 %.4f" % (len(a), float(a.mean()))

        def c_clean_flicker():
            fl = flicker_scan(clean_mp4)
            if fl:
                return False, "正常运动画面误报 %d 处：%s" % (len(fl), fl[:3])
            return True, "0 处误报"

        def c_clean_black():
            bw = black_windows(clean_mp4)
            if bw:
                return False, "正常画面误报空窗 %s" % (bw,)
            return True, "0 处误报"

        def c_flash():
            fl = flicker_scan(bad_mp4)
            if len(fl) != 1:
                return False, "应检出 1 处，实际 %d 处：%s" % (len(fl), fl[:4])
            e = fl[0]
            if abs(e["t"] - 5.5) > 0.06:
                return False, "t=%.3fs 偏离注入时刻 5.5s" % e["t"]
            if e["score"] < FLICKER_THRESH:
                return False, "score=%.1f 低于阈值" % e["score"]
            bb = e["bbox"]
            if not (len(bb) == 4 and 0 <= bb[0] < bb[2] <= 160 and 0 <= bb[1] < bb[3] <= 90):
                return False, "bbox 越界：%s" % (bb,)
            if e.get("ratio", 0.0) < FLICKER_ISO:
                return False, "孤立比值 %.2f 低于 %.2f" % (e.get("ratio", 0.0), FLICKER_ISO)
            return True, "t=%.3fs score=%.1f ratio=%.2f median=%.1f bbox=%s" % (
                e["t"], e["score"], e.get("ratio", 0.0), e.get("median", 0.0), bb)

        def c_black():
            bw = black_windows(bad_mp4)
            if len(bw) != 1:
                return False, "应检出 1 段，实际 %d 段：%s" % (len(bw), bw)
            w = bw[0]
            if abs(w["t0"] - 2.0) > 0.08 or abs(w["t1"] - 3.5) > 0.08:
                return False, "区间 %.2f–%.2fs 偏离注入的 2.0–3.5s" % (w["t0"], w["t1"])
            return True, "%.2f–%.2fs（1.50 s）" % (w["t0"], w["t1"])

        def c_head():
            bw = black_windows(bad_mp4)
            bad = [w for w in bw if w["t0"] < 1.0]
            if bad:
                return False, "片头 0.8 s 黑被误报：%s" % (bad,)
            return True, "片头 0.8 s 黑未误报（head=%.1fs）" % HEAD_SKIP

        def c_luma():
            L = luma_curve(bad_mp4)
            if not (238 <= len(L) <= 242):
                return False, "点数 %d（预期约 240）" % len(L)
            ts = [p["t"] for p in L]
            if any(b <= a for a, b in zip(ts, ts[1:])):
                return False, "时间轴不是递增"
            dark = [p["v"] for p in L if 2.5 <= p["t"] <= 3.4]
            bright = [p["v"] for p in L if 1.0 <= p["t"] <= 1.9]
            if not dark or max(dark) > 0.01:
                return False, "空窗段亮度 %.4f（应 < 0.01）" % (max(dark) if dark else -1)
            if not bright or min(bright) < 0.15:
                return False, "正常段亮度 %.4f（应 > 0.15）" % (min(bright) if bright else -1)
            return True, "%d 点；空窗段 max=%.4f，正常段 min=%.3f" % (
                len(L), max(dark), min(bright))

        def c_loud():
            lo = loudness_curve(bad_mp4)
            if len(lo) < 60:
                return False, "点数只有 %d" % len(lo)
            vals = sorted(p["lufs"] for p in lo)
            mid = vals[len(vals) // 2]
            if not (-40.0 <= mid <= -10.0):
                return False, "中位响度 %.1f 不在 [-40,-10]" % mid
            quiet = [p["lufs"] for p in lo if 1.6 <= p["t"] <= 2.4]
            if not quiet or min(quiet) > -45.0:
                return False, "1.5–2.5 s 静音段最小 %.1f（应 < -45）" % (
                    min(quiet) if quiet else 0.0)
            return True, "%d 点，中位 %.1f，静音谷 %.1f" % (len(lo), mid, min(quiet))

        def c_sync():
            cues = base_cues["cues"]
            events = base_cues["events"]
            out = sync_check(cues, events)
            if len(out) != 1:
                return False, "应报 1 条超差，实际 %d 条：%s" % (len(out), out)
            s = out[0]
            if s["cue"] != "whoosh":
                return False, "cue=%r（预期 whoosh）" % s["cue"]
            if abs(s["dt"] - 0.4) > 0.01:
                return False, "dt=%.3f（预期 0.4）" % s["dt"]
            keys = set(s)
            if keys != {"cue", "cue_t", "event_t", "dt"}:
                return False, "字段 %s 不符合 §4.5" % sorted(keys)
            good = sync_check([{"t": 2.0, "type": "impact"}],
                              [{"t": 2.05, "kind": "impact"}])
            if good:
                return False, "容差内（dt=0.05）被误报：%s" % (good,)
            return True, "whoosh dt=%+.3fs；容差内不误报" % s["dt"]

        def c_static_hold():
            """真的卡帧 6 s 必须报；5 s 定格（片尾定版卡级别）不报 —— 静止判据回归。"""
            hold = os.path.join(tmp, "hold6.mp4")
            short = os.path.join(tmp, "hold5.mp4")
            _encode_rawvideo(hold, _motion_frames(12.0, 30, 320, 180,
                                                  drift_at=3.0, drift_win=6.0), 30)
            _encode_rawvideo(short, _motion_frames(12.0, 30, 320, 180,
                                                   drift_at=3.0, drift_win=5.0), 30)
            rep = report(hold)
            got = [w for w in rep["warnings"] if "画面长时间静止" in w]
            if len(got) != 1:
                return False, "注入 6 s 卡帧应报 1 段，实际 %d：%s" % (len(got), got)
            # 报出的区间要覆盖注入段（首尾各留一帧容差）
            if "3.00–9.00s" not in got[0]:
                return False, "静止区间不对（应 3.00–9.00s）：%s" % got[0][:120]
            rep2 = report(short)
            got2 = [w for w in rep2["warnings"] if "画面长时间静止" in w]
            if got2:
                return False, "5 s 定格被误报成卡帧（阈值应为 6 s）：%s" % got2
            return True, "6 s 冻结报 1 段（3.00–9.00s）；5 s 定格不报"

        def c_anchor_sync():
            """cue 自带 visual_at 锚点时，PIL 路线（没有 events）也能自校验音画同步。"""
            cues = [{"t": 2.0, "type": "impact", "visual_at": 2.3},
                    {"t": 5.0, "type": "whoosh", "visual_at": 5.04}]
            evs = []
            got = sync_check(cues, evs)
            if len(got) != 1 or abs(got[0]["dt"] - 0.3) > 1e-6:
                return False, "带锚点的 cue 应报 1 条 dt=+0.3，实际 %s" % (got,)
            loaded, loaded_ev, warns, info = _load_cues(
                {"cues": [{"t": 2.0, "type": "impact", "visual_at": 2.3}], "events": []})
            if info["anchors"] != 1 or info["judged"] != 1:
                return False, "锚点统计不对：%s" % (info,)
            if any("没有 events 字段，音画同步对照跳过" in w for w in warns):
                return False, "有锚点却仍报'同步跳过'：%s" % (warns,)
            rep_ok = report(bad_mp4, cues_json={"cues": [
                {"t": 2.0, "type": "impact", "visual_at": 2.03}], "events": []})
            if any("音画同步" in e for e in rep_ok["errors"]):
                return False, "容差内锚点被误报：%s" % (rep_ok["errors"],)
            rep_bad = report(bad_mp4, cues_json={"cues": [
                {"t": 2.0, "type": "impact", "visual_at": 2.4}], "events": []})
            if not any("音画同步超差" in e for e in rep_bad["errors"]):
                return False, "锚点超差未被检出：%s" % (rep_bad["errors"],)
            # require_sync：锚点齐全 → 不升级；没有锚点也没有同名事件 → 升级为 error
            rep_rs_ok = report(bad_mp4, cues_json={"cues": [
                {"t": 2.0, "type": "impact", "visual_at": 2.0}]}, require_sync=True)
            if any("音画同步门禁未执行" in e for e in rep_rs_ok["errors"]):
                return False, "有锚点却被 require_sync 判成没查：%s" % (rep_rs_ok["errors"],)
            rep_rs_bad = report(bad_mp4, cues_json={"cues": [
                {"t": 2.0, "type": "impact"}], "events": []}, require_sync=True)
            if not any("音画同步门禁未执行" in e for e in rep_rs_bad["errors"]):
                return False, "require_sync 没把'没查'升级成 error：%s" % (rep_rs_bad["errors"],)
            if any("没有 events 字段" in w for w in rep_rs_bad["warnings"]):
                return False, "升级为 error 后 warning 没有去重：%s" % (rep_rs_bad["warnings"],)
            return True, "visual_at 锚点自校验（dt=+0.3 报错）；require_sync 升级/去重正确"

        def c_fastflash():
            """快速运动段 + 单帧白闪：不允许静默放过，至少进 warning 或 error。"""
            p = os.path.join(tmp, "fastflash.mp4")
            rng = np.random.default_rng(7)
            tex = rng.integers(40, 220, size=(180 + 40, 320 + 400)).astype(np.float32)
            frames = []
            for i in range(int(8.0 * 30)):
                t = i / 30.0
                xs = (np.arange(320) + int(round(i * 9.0))) % tex.shape[1]
                fr = tex[20:20 + 180][:, xs]
                if abs(t - 5.5) < 0.5 / 30.0:
                    fr = fr.copy()
                    fr[55:125, 90:160] = 255.0
                frames.append(np.clip(fr, 0, 255).astype(np.uint8))
            _encode_rawvideo(p, np.stack(frames), 30)
            rep = report(p)
            hit = [fl for fl in rep["flicker"] if abs(fl["t"] - 5.5) <= 0.1]
            sup = [w for w in rep["warnings"] if "高分异常被判为运动" in w]
            if hit:
                return True, "单帧白闪在快速运动段仍被检出 error（score=%.1f）" % hit[0]["score"]
            if sup:
                return True, "单帧白闪被运动闸拦下但进了 warning（风险可见）：%s" % sup[0][:80]
            return False, "快速运动段的单帧白闪被静默放过（无 error 也无 warning）"

        def c_isolation():
            """判据回归：连续小元素运动（近似 16.933 s 案例）不报；单帧尖峰要报；硬切不报。"""
            boxes = [((0, 0, 8, 8), 1.0)] * 32
            cont = np.array([0.0, 0.0, 9.6, 10.3, 6.6, 5.6, 6.5, 3.5, 5.0, 4.6, 2.2, 0.0, 0.0])
            got = _flicker_from_scores(cont, boxes, 30.0, FLICKER_THRESH)
            if got:
                return False, "连续运动/动画被误报：%s" % (got,)
            spike = np.array([0.5, 0.4, 178.0, 89.0, 0.3, 0.2])
            got2 = _flicker_from_scores(spike, boxes, 30.0, FLICKER_THRESH)
            if len(got2) != 1 or abs(got2[0]["t"] - 0.067) > 1e-6:
                return False, "单帧尖峰未检出：%s" % (got2,)
            if abs(got2[0]["score"] - 178.0) > 1e-6 or got2[0]["median"] != 1.0:
                return False, "字段不符：%s" % (got2,)
            cut = np.array([0.2, 40.0, 40.0, 0.2])
            got3 = _flicker_from_scores(cut, boxes, 30.0, FLICKER_THRESH)
            if got3:
                return False, "硬切被误报：%s" % (got3,)

            # "回到原位"闸：加速运动（三帧都在快变）要放过，且高分候选要进 suppressed 提醒复核
            acc_s = np.array([2.0, 2.0, 5.0, 9.7, 6.0, 2.0])
            acc_r = np.array([0.0, 0.0, 8.0, 20.0, 10.0, 0.0])
            sup = []
            got4 = _flicker_from_scores(acc_s, boxes, 30.0, FLICKER_THRESH,
                                        returns=acc_r, suppressed=sup)
            if got4:
                return False, "加速运动被误报成闪烁：%s" % (got4,)
            # 单帧尖峰：前后几乎相同（returns 很小）-> 仍要报
            sp_s = np.array([2.0, 2.0, 178.0, 89.0, 2.0, 2.0])
            sp_r = np.array([0.0, 0.0, 4.0, 2.0, 0.0, 0.0])
            sup2 = []
            got5 = _flicker_from_scores(sp_s, boxes, 30.0, FLICKER_THRESH,
                                        returns=sp_r, suppressed=sup2)
            if len(got5) != 1 or abs(got5[0]["t"] - 0.067) > 1e-6:
                return False, "加 returns 闸后单帧尖峰漏检：%s" % (got5,)
            if sup2:
                return False, "真闪烁被误收进 suppressed：%s" % (sup2,)
            # 高分但被判成运动 -> 必须进 suppressed（风险可见，不静默放过）
            hi_s = np.array([2.0, 2.0, 5.0, 140.0, 6.0, 2.0])
            hi_r = np.array([0.0, 0.0, 8.0, 300.0, 10.0, 0.0])
            sup3 = []
            got6 = _flicker_from_scores(hi_s, boxes, 30.0, FLICKER_THRESH,
                                        returns=hi_r, suppressed=sup3)
            if got6:
                return False, "高分运动候选被记成 error：%s" % (got6,)
            if len(sup3) != 1 or abs(sup3[0]["t"] - round(3 / 30.0, 3)) > 1e-6:
                return False, "高分运动候选没有进 suppressed（风险会静默）：%s" % (sup3,)
            return True, ("连续运动/硬切不报，单帧尖峰（ratio=2.0）报 1 处；"
                          "加速运动放过；高分运动候选进 suppressed（%d 条）" % len(sup3))

        def c_sync_naming():
            """events.kind 与 cue.type 全不同名时，必须给 warning —— 否则 sync 会静默全过。"""
            good = {"cues": [{"t": 1.0, "type": "impact"}],
                    "events": [{"t": 1.0, "kind": "impact"}]}
            _, _, w_good, info_good = _load_cues(good)
            if any("音画同步检查可能没生效" in w for w in w_good):
                return False, "同名事件被误报：%s" % (w_good,)
            if info_good.get("judged") != 1:
                return False, "judged 统计错（应 1）：%s" % (info_good,)
            bad = {"cues": [{"t": 1.0, "type": "impact"}],
                   "events": [{"t": 1.0, "kind": "flash"}]}
            _, _, w_bad, info_bad = _load_cues(bad)
            if not any("音画同步检查可能没生效" in w for w in w_bad):
                return False, "异名事件没有触发守卫（sync 会静默全过）：%s" % (w_bad,)
            if info_bad.get("judged") != 0 or not info_bad.get("blockers"):
                return False, "异名事件的 judged/blockers 不对：%s" % (info_bad,)
            return True, "同名不误报；异名触发'可能没生效'的 warning（judged=0）"

        out_json = os.path.join(tmp, "deep.json")
        out_png = os.path.join(tmp, "deep.png")

        def c_report():
            rep = report(bad_mp4, cues_json=cues_plain, expect_duration=8.0,
                         out_json=out_json, out_png=out_png)
            keys = {"flicker", "black_windows", "luma", "loudness", "sync",
                    "errors", "warnings"}
            # sync_info 是报告里的**增量**字段（render.py 的 G9 从 deep["sync_info"]["judged"]
            # 读"同步到底判了几条"，缺了它 -> QC 报告永远显示"同步未判"）。
            # 契约 §4.5 的 7 个键必须齐全，允许额外的只读统计键。
            if not keys.issubset(set(rep)):
                return False, "顶层字段 %s 缺 §4.5 键" % sorted(keys - set(rep))
            if not isinstance(rep.get("sync_info"), dict):
                return False, "sync_info 缺失或不是 dict：%r" % (rep.get("sync_info"),)
            if len(rep["flicker"]) != 1 or rep["flicker"][0].get("expected") is not False:
                return False, "flicker=%s" % (rep["flicker"],)
            if len(rep["black_windows"]) != 1:
                return False, "black_windows=%s" % (rep["black_windows"],)
            if len(rep["sync"]) != 1:
                return False, "sync=%s" % (rep["sync"],)
            kinds = {"单帧闪烁": False, "画面空窗": False, "音画同步超差": False}
            for e in rep["errors"]:
                for k in kinds:
                    if k in e:
                        kinds[k] = True
            if not all(kinds.values()):
                return False, "分档缺项 %s；errors=%s" % (kinds, rep["errors"])
            if rep["loudness"] and len(rep["loudness"]) < 60:
                return False, "响度曲线过短"
            try:
                json.dumps(rep, allow_nan=False)
            except Exception as e:      # noqa: BLE001
                return False, "JSON 序列化失败（含 NaN？）：%s" % e
            return True, "闪烁/空窗/同步三档 error 齐全，%d 个 warning" % len(rep["warnings"])

        def c_expected():
            rep = report(bad_mp4, cues_json=cues_flash, expect_duration=8.0)
            fl = rep["flicker"]
            if len(fl) != 1 or fl[0].get("expected") is not True:
                return False, "设计事件未标记 expected：%s" % (fl,)
            if any("单帧闪烁" in e for e in rep["errors"]):
                return False, "设计事件仍记 error：%s" % (rep["errors"],)
            if not any("已排除" in w for w in rep["warnings"]):
                return False, "缺少'已排除'warning"
            return True, "闪白事件被解释，不记 error"

        def c_expect_dur():
            rep = report(bad_mp4, cues_json=cues_plain, expect_duration=9.9)
            if not any("时长" in e for e in rep["errors"]):
                return False, "时长偏差未报 error：%s" % (rep["errors"],)
            return True, "预期时长偏差 %.2fs 被检出" % 1.9

        def c_files():
            if not os.path.exists(out_json) or os.path.getsize(out_json) < 500:
                return False, "deep.json 缺失或过小"
            if not os.path.exists(out_png) or os.path.getsize(out_png) < 20000:
                return False, "deep.png 缺失或过小"
            with open(out_json, encoding="utf-8") as fp:
                data = json.load(fp)
            need = {"flicker", "black_windows", "luma", "loudness", "sync",
                    "errors", "warnings"}
            if not need.issubset(set(data)):
                return False, "JSON 字段不符（缺 %s）" % sorted(need - set(data))
            img = Image.open(out_png)
            img.load()
            if img.width < 1000 or img.height < 600:
                return False, "PNG 尺寸 %sx%s 过小" % (img.width, img.height)
            return True, "JSON %.1f KB / PNG %dx%d %.1f KB" % (
                os.path.getsize(out_json) / 1024.0, img.width, img.height,
                os.path.getsize(out_png) / 1024.0)

        def c_cli():
            cli_json = os.path.join(tmp, "cli_deep.json")
            cli_png = os.path.join(tmp, "cli_deep.png")
            p = subprocess.run(
                [sys.executable, os.path.join(HERE, "qa_frames.py"), bad_mp4,
                 "--cues", cues_plain, "--out", cli_json, "--png", cli_png],
                cwd=HERE, capture_output=True, timeout=120)
            out = (p.stdout or b"").decode("utf-8", "replace")
            if p.returncode != 1:
                return False, "有 error 时 CLI 应 exit 1，实际 %d；stderr=%s" % (
                    p.returncode, (p.stderr or b"").decode("utf-8", "replace")[-200:])
            if "闪烁" not in out or "画面空窗" not in out:
                return False, "CLI 输出缺少中文摘要：%r" % out[:200]
            if not (os.path.exists(cli_json) and os.path.exists(cli_png)):
                return False, "CLI 未写出 JSON/PNG"
            return True, "exit=1（有 error），中文摘要正常，文件已写出"

        def c_import():
            p = subprocess.run([sys.executable, "-c", "import qa_frames"],
                               cwd=HERE, capture_output=True, timeout=60)
            if p.returncode != 0:
                return False, "import 失败：%s" % (
                    (p.stderr or b"").decode("utf-8", "replace")[-300:])
            if (p.stdout or b"").strip():
                return False, "import 有打印：%r" % (p.stdout[:200],)
            return True, "干净退出，无打印"

        check("ffmpeg 可执行", c_ffmpeg)
        check("sample_frames 形状/类型/范围", c_sample)
        check("sample_frames from_s/to_s 时间窗", c_window)
        check("正常运动画面不误报闪烁", c_clean_flicker)
        check("正常运动画面不误报空窗", c_clean_black)
        check("注入单帧白闪被检出", c_flash)
        check("注入 1.5 s 黑窗被检出", c_black)
        check("片头黑窗不误报", c_head)
        check("luma_curve 时间轴与暗场", c_luma)
        check("loudness_curve 能测到静音谷", c_loud)
        check("sync_check 只报超差", c_sync)
        check("孤立性判据回归（连续运动不误报）", c_isolation)
        check("卡帧判据：6 s 冻结报出、5 s 定格不报", c_static_hold)
        check("快速运动段单帧白闪不静默", c_fastflash)
        check("cue 自带 visual_at 锚点的同步自校验", c_anchor_sync)
        check("report 三档 error + §4.5 字段", c_report)
        check("report 排除已登记设计事件", c_expected)
        check("音画同步命名约定守卫", c_sync_naming)
        check("report 预期时长校验", c_expect_dur)
        check("report 写出 JSON + PNG", c_files)
        check("CLI 端到端", c_cli)
        check("import 无副作用", c_import)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    elapsed = time.perf_counter() - t_start
    print("== 结果：%d/%d PASS，耗时 %.1f s" % (npass, total, elapsed))
    if npass != total:
        print("   [FAIL] 有 %d 项未通过" % (total - npass))
        return 1
    print("   [PASS] 全部通过")
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="qa_frames.py",
        description="帧级 / 时间轴级深度质检：单帧闪烁、画面空窗、音画同步")
    ap.add_argument("film", nargs="?", help="成片路径（.mp4 等）")
    ap.add_argument("--cues", default=None,
                    help="work/cues.json（§4.2：cues 音频提示点 + events 视觉事件）")
    ap.add_argument("--shots", default=None,
                    help="镜头切点（timeline.json 或秒数列表）；切点附近 ±0.12s 的孤立尖峰"
                         "降级为 warning（素材线硬切会被误判成单帧闪烁）")
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径（缺省不写文件）")
    ap.add_argument("--png", default=None, help="报告 PNG 输出路径（缺省不写文件）")
    ap.add_argument("--expect-duration", type=float, default=None,
                    help="预期成片时长（秒），偏差 > 0.5 s 记 error")
    ap.add_argument("--require-sync", action="store_true",
                    help="音画同步门禁未能执行时记 error（默认只记 warning；"
                         "也可用环境变量 QA_REQUIRE_SYNC=1 全局开启，render.py 的深检会自动继承）")
    ap.add_argument("--selftest", action="store_true", help="跑内置自检（不联网，≤60 s）")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.film:
        ap.error("需要成片路径（或用 --selftest）")
    _utf8_stdout()
    try:
        rep = report(a.film, cues_json=a.cues, shots_json=a.shots,
                     expect_duration=a.expect_duration,
                     out_json=a.out, out_png=a.png, require_sync=a.require_sync)
    except Exception as e:              # noqa: BLE001
        print("[FAIL] 深度质检失败：%s" % e, file=sys.stderr)
        return 1
    _print_summary(rep, a.film, a.out, a.png)
    return 1 if rep.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
