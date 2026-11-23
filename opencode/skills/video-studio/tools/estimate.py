# -*- coding: utf-8 -*-
"""estimate.py — 成本/时长预演：跑 --stage all 之前，先算"要渲多久、占多少盘、怎么分块"。

用法
----
    python tools/estimate.py --script film.json [--work work] [--jobs 4]
                             [--engine pil|web] [--rerender 1.4] [--json]

做四件事
--------
  1. 帧数 / 时长 / fps / 分辨率（分辨率 1920x1080 在本项目写死，见 filmkit.W/H）
  2. 各阶段耗时：tts / layout / cues / score / sfx / audio / frames / mux / qa
  3. 磁盘：work 下 raw 管道峰值 + 成片体积估算
  4. 分块建议（与 render.py 的 chunk_frames 规则逐字一致）+ 最大瓶颈项

输入口径
--------
  有 <work>/timeline.json（已排过轴）→ 用真实 total/frames，输出里注明"真实帧数"；
  没有 → 按 lines 文本"等效字数 ÷ 4.2 字/秒 + lead/tail + hold"粗估，
  并在输出里**明确标注这是估算**（排过轴再跑一次就会换成真实数字）。

退出码
------
  0 正常；2 剧本不存在 / 不是合法 JSON（一句话错误，不甩 traceback）。
  --json 时 stdout 只有 JSON（键名全英文），可直接 json.loads；警告走 stderr。

基准来源
--------
  所有速度常量集中在下面"基准常量"一节，出处是 skills/video-studio/SKILL.md §12
  （作者机器 2026-11-07 实测：Ryzen AI 9 HX 370 / 12C24T / 无 NVIDIA）。
  换机后先重测再采信——这些数字决定预演准不准，别凭感觉调。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import unicodedata
from pathlib import Path

# ============================================================================
# 基准常量（换机请重测后回填，别把这里的数字当通用真理）
# ============================================================================

W, H, FPS = 1920, 1080, 30          # 默认横屏画布；实际值运行时从 layoutkit 读（见 _canvas()）
PIL_FPS = (40.0, 80.0)              # PIL 逐帧渲染速度：中等复杂度 40–54 fps / 夹具片 68–82 fps
# 竖屏（9:16）的耗时系数：**实测 1.23×**（本文件其它常量都是实测值，这个也是）。
# 为什么不是 1.0：竖屏与横屏像素数相同（1080x1920 vs 1920x1080），但绘制调用更密——
# 10 类构图里 list/compare/process/chart 都变成"多个纵向块"，填充面积接近而调用次数更多。
# 测法：同一套 10 类构图场景 + 每条都叠字幕，两个 profile 各渲 120 帧取墙钟；
# 横屏 174.5 fps / 竖屏 141.5 fps → 1.23×。**比的是比值不是绝对值**：夹具内容偏简单，
# 绝对帧率远高于 PIL_FPS 的 40–80（那是中等复杂度真实片的口径），但比值与内容复杂度弱相关。
# 换机/换场景后这个比值可能漂，漂到 1.5 以上再重测。
PORTRAIT_COST_FACTOR = 1.23
PARALLEL_EFF = 0.5                  # 分块并行的**实测效率**：2 进程只快 1.5×，不是 2×（见 CALIBRATION）
WEB_SEC_PER_FRAME = (0.080, 0.130)  # web 引擎：Edge 无头 PNG 截图 80–130 ms/帧
WEB_STARTUP_SEC = 6.0               # 浏览器冷启动 + CDP 连接（12 s 片 28–30 s 含启动）
X264_FPS = 141.0                    # libx264 -preset medium -crf 17 ≈ 4.7× 实时（仅 reencode_video 用）
TTS_SEC_PER_LINE = (2.0, 4.0)       # edge-tts：每句每 worker 的墙钟（联网，波动大）：实测 2.2 s，留到 4.0
# 配乐耗时锚点 (片长 s, 耗时低, 耗时高)。
#
# musicgen 实测值：30 s / 60 s / 120 s 分别 0.94 / 1.93 / 3.68 s（≈31× 实时）。
# 参数化编排器是单遍事件合成，实测抖动也小（同参数逐采样确定，耗时只随片长线性涨）；
# 早期逐层展开事件、多次 IIR 的配乐引擎实测 29 s 片要 3.6–6.5 s。
# 区间仍给 0.7–1.5 系数包络：CI 机器与共享 CPU 会慢，报小了会让人以为卡住。
SCORE_REFS = ((29.0, 0.9, 1.6), (58.0, 1.8, 3.0), (389.0, 11.5, 19.0))
SFX_SEC_PER_CUE = 0.20              # 单条短音效 合成+落位（24 配方单次 ≤1.5 s 是上限口径）
SFX_MIN_SEC = 0.8                   # 实测 18 条 cue 的夹具片 sfx 墙钟 0.8 s（含 ffmpeg 写盘）
LAYOUT_BASE, LAYOUT_PER_LINE = 0.05, 0.01  # 排轴 + srt/ass + 安全区/字形自查（实测 29 s 片 <0.15 s）
CUES_BASE, CUES_PER_LINE = 0.05, 0.005     # 提示点落盘 + 排序（实测 29 s 片 <0.15 s）
AUDIO_BASE, AUDIO_PER_SEC = 0.6, 0.012     # numpy 总线按采样点叠加 + 写 wav
# mux 默认是 -c:v copy + 两遍 loudnorm：实测 29 s 片 1.7–1.9 s，与帧数无关、跟片长弱相关。
MUX_BASE_SEC = 0.6                  # 起 ffmpeg 两遍 + 封装
MUX_PER_SEC = 0.045                 # 每片长秒（两遍 loudnorm 解码音频）
# QA：实测 29 s 片 6.2–6.8 s（含联系表 + 锐度参考帧重渲染 + 帧级深检）。原来的 1/30 把 qa 算了 6 倍小。
QA_BASE_SEC = (1.0, 3.0)            # 固定开销：联系表 + 参考帧重渲染
QA_SEC_PER_SEC = (0.12, 0.25)       # 随片长：帧级深检流式解码 + 响度曲线
UNMEASURED_BAND = 0.30              # 没有实测的阶段（layout/cues/sfx/audio）给 ±30% 的区间，别假装精确
# G11（全片 VMAF/SSIM）：参考帧由场景函数**重渲一遍**再喂 libvmaf，成本跟帧数走、
# 与 frames 阶段同量级。实测口径差异极大：qavmaf 作者 29 s 片 12.0 s（≈0.40× 片长）；
# SKILL.md §12 同一部夹具片 14.2 s；同机另一轮负载下 28.8 s。这里取 0.40–1.00× 片长
# （下限=干净机、上限=争 CPU 的长片外推），固定开销单独列 1–2 s。
# 注：1.0–1.6× 这类没有实测支撑的系数会把 29 s 片算成 31–50 s（实际 12–29 s），
# 预演总量系统性偏大近 2 倍——"拍脑袋的悲观系数"比低估更糟：排期会按假的数字走。
VMAF_BASE_SEC = (1.0, 2.0)          # 起渲染进程 + 参考帧管线搭建
VMAF_SEC_PER_SEC = (0.40, 1.00)     # 每片长秒（参考帧重渲 + libvmaf 逐帧比）

# 校准出处：换成你自己的机器前，请按 SKILL.md §12 重测并回填——这些数字决定预演准不准。
CALIBRATION = dict(
    machine=("AMD64 Family 26 Model 36 Stepping 0（24 逻辑核）/ Windows；"
             "SKILL.md §12 记录的作者机是 Ryzen AI 9 HX 370 / 12C24T / Radeon 890M"
             "（是否同一台未核实，只保证这些数字是在上述 24 逻辑核机器上测的）"),
    measured_at="2026-11-10",
    sdk="video-studio scripts（ffmpeg 自带 x264）",
    film="29.011 s / 870 帧 / 测试样片（assets/film.json + assets/scenes.py，仅用于基准校准）",
    method="render.py --stage all --jobs 2，逐 stage 取 [time] 行墙钟；同片重复 2 次",
    observed=dict(tts=2.2, layout=0.0, cues=0.0, score=6.3, sfx=0.8, audio=0.7,
                  frames=9.7, mux=1.9, qa=6.8, total=28.4),
    extra="frames: 870 帧 jobs=1 14.37 s / jobs=2 9.53 s（并行效率 1.51×）;"
          " score 58 s 片 4.5–5.0 s; qa 6.2–6.8 s",
)
CPS = 4.2                            # 每秒约 4.2 字（voicekit 校准 3.84–4.61 cps 的中位口径）
DEFAULT_LEAD, DEFAULT_TAIL, DEFAULT_MIN_DUR = 0.62, 0.92, 3.0  # body 基准
CORES = 12                           # HX 370：12C/24T；jobs > 12 不再线性加速
TTS_BYTES_PER_LINE = 32 * 1024       # 单句 mp3 缓存（4 s 语音约 24 KB，留余量）
WAV_BYTES_PER_SEC = 48000 * 2 * 2    # 48 kHz 立体声 PCM16 = 192 KB/s
VIDEO_MBPS = (1.2, 3.0)              # crf17 @1080p30 图文内容（暗底大字 0.5–1 Mbps 属正常）
AUDIO_MBPS = 0.192                   # AAC 192 kbps
QA_DISK_BYTES = 12 * 1024 * 1024     # 联系表 + 静帧 + deep.png
BASELINE_NOTE = ("区间来自本机实测（机型/样本见下方 calibration；"
                 "SKILL.md §12 记录的作者机是 Ryzen AI 9 HX 370 / 12C24T）；换机请先重测再采信")

def _canvas():
    """当前画布与是否为竖屏。

    优先问 layoutkit（它才是真源），拿不到就按环境变量判，最后退回横屏常量。
    三条路都不抛异常：estimate 是"开渲前预演"，它自己不该因为取不到画布而崩。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import layoutkit                                # noqa: PLC0415
        return int(layoutkit.W), int(layoutkit.H)
    except Exception:                                   # noqa: BLE001
        prof = str(os.environ.get("VIDEO_STUDIO_LAYOUT") or "").strip().lower()
        if prof in ("portrait", "9x16", "vertical", "portrait-9x16"):
            return 1080, 1920
        return W, H


# 当前画布（模块级）：import 时定一次（见下面的 _CANVAS = _canvas()）。
# 与 scenes/filmkit 同一约定：改画布必须在导入前用 VIDEO_STUDIO_LAYOUT 定死。
# 这里不再预置一个占位常量：占位值会被下一行真值覆盖，留着只会让人以为"改这里有效"。


def _cost_factor(w, h):
    """非横屏的耗时上浮系数（竖屏按 PORTRAIT_COST_FACTOR 上浮，见该常量的注释）。"""
    return PORTRAIT_COST_FACTOR if int(h) > int(w) else 1.0


_CANVAS = _canvas()


STAGE_LABELS = {
    "tts": "旁白 edge-tts",
    "layout": "排时间轴",
    "cues": "画面提示点",
    "score": "程序化配乐",
    "sfx": "音效合成",
    "audio": "三总线混音",
    "frames": "逐帧渲染+编码",
    "mux": "合成成片",
    "qa": "质检+抽帧",
    # 交付前的三道门禁单独列（它们的成本不能藏进 qa 里）
    "preflight": "渲染前预检",
    "vmaf": "全片VMAF/SSIM",
    "asr": "ASR回读",
}


# ============================================================================
# 小工具
# ============================================================================

def _setup_stdio():
    """输出被管道/文件重定向时把 stdout/stderr 切到 UTF-8（与 render.py 同一口径）。

    Windows 下 python tools/estimate.py ... | tee log 的 stdout 默认是 GBK 代码页，
    中文会乱码；真实控制台保持系统编码，交互运行时中文照样正常。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _die(msg, as_json=False):
    """一句话错误 + 退出码 2（输入/用法错误，不是片子坏了）。"""
    if as_json:
        sys.stdout.write(json.dumps({"ok": False, "error": msg}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write("[estimate] %s\n" % msg)
    raise SystemExit(2)


def _warn(msg):
    """预演不阻断：警告一律走 stderr，--json 时 stdout 仍然只有 JSON。"""
    sys.stderr.write("[estimate][WARN] %s\n" % msg)


def load_script(path, as_json=False):
    """读剧本；坏输入给一句话错误（退出码 2），不甩 traceback。"""
    p = Path(path).expanduser()
    if not p.exists():
        _die("找不到剧本 %s（--script 指向你自己的 film.json；"
             "仓库里只有一个用于测试的样例 skills/video-studio/assets/film.json，"
             "它不是要抄的模板）" % p, as_json)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        _die("读不了剧本 %s：%s" % (p, e), as_json)
    except UnicodeDecodeError as e:
        _die("%s 不是 UTF-8 文本（%s）：用 UTF-8 另存 film.json" % (p, e), as_json)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _die("%s 不是合法 JSON：第 %d 行第 %d 列 %s（先跑 python -m json.tool 看一眼）"
             % (p, e.lineno, e.colno, e.msg), as_json)
    if not isinstance(data, dict):
        _die("%s 顶层必须是 JSON 对象（当前是 %s）" % (p, type(data).__name__), as_json)
    return p, data


def _num(v, default=0.0):
    """宽容取数：film.json 里写成字符串/None 时退回默认值（估算不该被脏字段炸掉）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _weighted_chars(text):
    """等效字数：汉字 1；英文/数字 0.6；标点 0.3（停顿）；空格 0。"""
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


def _dw(s):
    """显示宽度（中文按 2 列算），只用于表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _dw(s))


def _fmt_bytes(n):
    """十进制单位（1 GB = 10^9 B）：和 SKILL.md §12 的口径一致（1080p30 的 30 s ≈5.6 GB）。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000.0 or unit == "TB":
            if unit == "B":
                return "%.0f B" % n
            return "%.1f %s" % (n, unit)
        n /= 1000.0


def _fmt_span(lo, hi):
    if abs(float(hi) - float(lo)) < 0.05:
        return "%.1f s" % float(lo)
    return "%.1f–%.1f s" % (float(lo), float(hi))


def _qavmaf_sample_step(vcfg=None):
    """qavmaf 的抽样步长（sample 模式每 N 帧取 1 帧）。

    优先级与 render.py 的 _qa_vmaf_gate 一致：先读 film.json 的 qa.vmaf.sample_step，
    没写才用 qavmaf.SAMPLE_STEP。预演不另抄一份常量，否则改一处漏一处；而"用户显式
    写了 sample_step=20 却按默认 5 预演"会把这道门禁的成本高估 4 倍。
    读不到 qavmaf 时按 1（=full）保守估——那种环境下 render.py 的 G11 本来也会记
    skipped（没有模块），多估不会误导排期，少估才会。
    """
    try:
        if isinstance(vcfg, dict) and vcfg.get("sample_step") is not None:
            return max(1, int(vcfg["sample_step"]))
    except (TypeError, ValueError):
        pass
    try:
        scripts = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import qavmaf
        return max(1, int(qavmaf.SAMPLE_STEP))
    except Exception:                                       # noqa: BLE001
        return 1


# ============================================================================
# 估算核心
# ============================================================================

def estimate_timeline(script):
    """没有 timeline.json 时：按"等效字数 ÷ 4.2 字/秒 + lead/tail + hold"粗估。

    与 filmkit.build_timeline 的公式同构：dur = max(min_dur, lead + voice + tail + hold)。
    只能说"量级对"，误差通常 ±10–20%——这正是输出里必须标注"估算"的原因。
    """
    lines = [ln for ln in (script.get("lines") or []) if isinstance(ln, dict)]
    lead = _num(script.get("lead", DEFAULT_LEAD), DEFAULT_LEAD)
    tail = _num(script.get("tail", DEFAULT_TAIL), DEFAULT_TAIL)
    min_dur = _num(script.get("min_dur", DEFAULT_MIN_DUR), DEFAULT_MIN_DUR)
    ghold = _num(script.get("hold", 0.0))
    scenes = []
    for ln in lines:
        voice = _weighted_chars(ln.get("text")) / CPS
        hold = _num(ln.get("hold", ghold))
        scenes.append(dict(id=str(ln.get("id") or ""), voice=voice, hold=hold,
                           dur=max(min_dur, lead + voice + tail + hold)))
    return sum(s["dur"] for s in scenes), scenes


def read_timeline(work):
    """读 <work>/timeline.json（已排轴的真实帧数）。缺失/坏文件返回 (None, 路径)。"""
    p = Path(work) / "timeline.json"
    if not p.exists():
        return None, p
    try:
        tl = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _warn("忽略 %s（读不了：%s），退回剧本粗估" % (p, e))
        return None, p
    if not isinstance(tl, dict) or "total" not in tl or "frames" not in tl:
        _warn("忽略 %s（没有 total/frames 字段，不像本项目的 timeline），退回剧本粗估" % p)
        return None, p
    # 字段存在但值不可用（字符串/None/0/负数/NaN）同样是坏 timeline：build_report 会把
    # 这三个数原样写进报告，坏值不会抛错，只会变成 frames=0 / duration=0.0 的"真实帧数"，
    # 或者 fps=-30 算出负的分块与负段数——静默失败比崩溃更难查，所以在这里拦下来。
    bad = []
    for key in ("total", "frames") + (("fps",) if "fps" in tl else ()):
        try:
            v = float(tl[key])
        except (TypeError, ValueError):
            bad.append("%s=%r" % (key, tl[key]))
            continue
        if not math.isfinite(v) or v <= 0:
            bad.append("%s=%r" % (key, tl[key]))
    if bad:
        _warn("忽略 %s（%s 不是有限正数，不像本项目的 timeline），退回剧本粗估"
              % (p, "、".join(bad)))
        return None, p
    return tl, p


def score_seconds(total):
    """配乐耗时：以 29 s / 58 s / 389 s 三个实测点线性折算（超出范围按最近一段斜率外推）。"""
    pts = [(r[0], r[1], r[2]) for r in SCORE_REFS]
    if total <= pts[0][0]:
        (x0, l0, h0), (x1, l1, h1) = pts[0], pts[1]
    elif total >= pts[-1][0]:
        (x0, l0, h0), (x1, l1, h1) = pts[-2], pts[-1]
    else:
        (x0, l0, h0), (x1, l1, h1) = pts[0], pts[1]
        for i in range(len(pts) - 1):
            if pts[i][0] <= total <= pts[i + 1][0]:
                (x0, l0, h0), (x1, l1, h1) = pts[i], pts[i + 1]
                break
    k = (float(total) - x0) / float(x1 - x0) if x1 != x0 else 0.0
    lo = l0 + (l1 - l0) * k
    hi = h0 + (h1 - h0) * k
    return max(0.3, lo), max(0.5, hi)


def _speedup(jobs):
    """分块并行的**有效加速比**：不是 jobs 倍。

    实测（870 帧夹具片）：jobs=1 → 14.37 s、jobs=2 → 9.53 s，只有 1.51×。
    原因很实在：父进程要 spawn 子进程、每段都要重新 import scenes.py / 解析主题、
    落盘 mux 串行、磁盘写带宽争用。按 jobs 线性折算会持续低估长片（越长的片分块越多）。
    这里用 1 + (jobs-1)*PARALLEL_EFF 的线性模型：比理论值保守，实测 2 进程刚好对上。
    """
    jobs_eff = max(1, min(int(jobs), CORES))
    return 1.0 + (jobs_eff - 1) * PARALLEL_EFF


def _fps_label(engine):
    """帧耗时基准的人读标签：竖屏要把"没实测的系数"写在脸上，别让人以为那是实测值。"""
    k = _cost_factor(_CANVAS[0], _CANVAS[1])
    base = ("PIL 40–80 fps" if engine == "pil"
            else "web 80–130 ms/帧 + 启动 6 s")
    if k == 1.0:
        return base
    return "%s × 竖屏系数 %.2f（实测比值，见 PORTRAIT_COST_FACTOR）" % (base, k)


def frames_seconds(frames, jobs, engine, rerender):
    """frames 阶段墙钟（只算画面生成；不重编码时封装成本在 mux 行）。

    PIL：帧数 ÷ 渲染 fps ÷ 有效加速比；web：启动开销 + 帧数 × 每帧截图耗时 ÷ 加速比。
    render.py 的分块 worker 是边渲染边编码，所以这里的 fps 已经含了编码开销。
    """
    sp = _speedup(jobs)
    # 竖屏的绘制调用更密（像素数相同），按 PORTRAIT_COST_FACTOR 上浮；
    # 横屏系数恒为 1.0，所以既有预演数字一个都不变。
    # 这个系数是实测比值（见常量处注释），输出里会单独标明。
    k = _cost_factor(_CANVAS[0], _CANVAS[1])
    if engine == "web":
        base = [WEB_STARTUP_SEC + frames * spf * k / sp for spf in WEB_SEC_PER_FRAME]
    else:
        base = [frames / (rfps / k) / sp for rfps in PIL_FPS]
    base = sorted(base)
    return base[0] * rerender, base[-1] * rerender


def mux_seconds_pair(frames, jobs, total, script):
    """mux 阶段区间：默认 -c:v copy 只跟片长有关；reencode_video=true 才跟帧数有关。"""
    reencode = bool(script.get("reencode_video", False))
    if reencode:
        v = frames / X264_FPS / _speedup(jobs)
        return v * 0.8, v * 1.6
    base = MUX_BASE_SEC + MUX_PER_SEC * max(0.0, float(total))
    return base * 0.75, base * 1.35


def chunk_plan(frames, fps, jobs, explicit):
    """逐字复刻 render.py 的分块规则（改了那边记得同步这里）。

        chunk = script.chunk_frames or round(frames/jobs/fps/4)*4*fps
        chunk = max(fps*2, round(chunk/fps)*fps)
    """
    fps = int(fps) or FPS
    jobs = max(1, int(jobs))
    if explicit:
        chunk = int(explicit)
    else:
        chunk = int(round(frames / float(jobs) / float(fps) / 4.0)) * 4 * fps
    chunk = max(fps * 2, int(round(chunk / float(fps))) * int(fps))
    nseg = int(math.ceil(frames / float(chunk))) if frames and chunk else 0
    return chunk, nseg


def disk_estimate(frames, total, n_lines, n_cues, crf, music_on):
    """磁盘：raw 管道峰值（不落盘但决定内存/管道带宽）+ work/ + out/。"""
    try:
        crf = int(crf)
    except (TypeError, ValueError):
        crf = 17
    factor = 2.0 ** ((17.0 - crf) / 6.0)          # crf 每降 6 体积约翻倍（x264 经验值）
    vlo = max(0.3, VIDEO_MBPS[0] * factor)
    vhi = max(0.3, VIDEO_MBPS[1] * factor)
    vid_hi = total * vhi * 1e6 / 8.0
    out_lo = total * (vlo + AUDIO_MBPS) * 1e6 / 8.0
    out_hi = total * (vhi + AUDIO_MBPS) * 1e6 / 8.0
    n_wav = 1 + (1 if music_on else 0) + (1 if n_cues else 0)
    wav_bytes = total * WAV_BYTES_PER_SEC
    segments = vid_hi                        # 分块文件合计 ≈ 整片视频体积
    silent = vid_hi
    tts = n_lines * TTS_BYTES_PER_LINE
    qa = QA_DISK_BYTES if frames else 0
    work = tts + segments + silent + n_wav * wav_bytes + qa
    return dict(raw_peak_bytes=W * H * 3 * int(frames),
                work_bytes=work,
                out_bytes_lo=out_lo, out_bytes_hi=out_hi,
                breakdown=dict(tts_bytes=tts, segments_bytes=segments,
                               silent_bytes=silent, wav_bytes=n_wav * wav_bytes,
                               qa_bytes=qa),
                video_mbps=(vlo, vhi))


def bottleneck_advice(stage, engine, jobs_eff):
    if stage == "frames":
        if engine == "web":
            return ("换 engine=pil：同一画面语言下 PIL 比浏览器逐帧快 5–10 倍；"
                    "确实需要 Canvas/WebGL 才留 web（--jobs 提到 %d 也压不动截图往返的瓶颈）"
                    % CORES)
        return ("把 --jobs 从 %d 提到 8–%d（本机 12 核）能把 frames 大致线性压下来；"
                "再想快只能减帧或简化场景——1920x1080 是写死的，降分辨率不是选项"
                % (jobs_eff, CORES))
    if stage == "tts":
        return "TTS 是网络等待：tts_workers 保持 4–6 就够，句子多先压台词，别靠并发硬堆"
    if stage == "qa":
        return "质检已是秒级；它随片长线性涨，是必要成本（不查等于没门禁）"
    if stage == "score":
        return "配乐是秒级纯 CPU 合成；片长再长也不该成为瓶颈"
    return "该阶段占比高但绝对值不大：按上面的区间排期即可"


# ============================================================================
# 主流程
# ============================================================================

def build_report(script_path, script, args):
    as_json = bool(args.as_json)
    lines = [ln for ln in (script.get("lines") or []) if isinstance(ln, dict)]
    if not lines:
        _die("%s 的 lines 是空的：至少写一句（render.py 只接受非空数组）" % script_path, as_json)

    engine = str(args.engine or script.get("engine") or "pil").lower()
    if engine not in ("pil", "web"):
        _warn("engine=%r 不认识（只能是 pil / web），按 pil 预演" % engine)
        engine = "pil"

    # 画幅/帧率可以按片配置（只允许 16:9 横屏 + 下采样输出，见 filmkit.video_plan）。
    # 预演必须跟着走，否则 720p 片会被按 1080p 估帧数与磁盘。
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import filmkit as _FK
        _vp = _FK.video_plan(script)
    except ImportError as e:
        # 只把"缺 filmkit"当降级：那是能用模板常量继续估的环境问题。
        _warn("装载不了 filmkit（%s），画幅/帧率按 %dx%d@%d 估" % (e, W, H, FPS))
        _vp = dict(fps=FPS, w=W, h=H, out_w=W, out_h=H,
                   label="%dx%d" % (W, H))
    except Exception as e:                                  # noqa: BLE001
        # video 块写错（非 16:9 / 非法 fps / 上采样）时 render.py 会在 st_layout 直接报错，
        # 预演却静默按默认值算并打印 ok:true——那等于告诉用户"能渲"，必须显式说清楚。
        _warn("film.json 的 video 块不合法（%s: %s）：render.py 会拒绝这部片子，"
              "下面的画幅/帧率只是占位估算" % (type(e).__name__, e))
        _vp = dict(fps=FPS, w=W, h=H, out_w=W, out_h=H,
                   label="%dx%d" % (W, H))
    tl, tl_path = read_timeline(args.work)
    if tl is not None:
        total = _num(tl.get("total"))
        fps = int(_num(tl.get("fps"), _vp["fps"])) or _vp["fps"]
        frames = int(_num(tl.get("frames"), round(total * fps)))
        source = "timeline"
        source_note = "真实帧数：%s" % tl_path
    else:
        total, _scenes = estimate_timeline(script)
        # 没有 timeline.json 时也必须用 film.json 的 video.fps —— 定死 30 会让
        # 24/25/50/60 fps 的片子帧数、磁盘、分块全部按 30 fps 算（24 fps 高估 25%，
        # 60 fps 低估一半），而 render.py 的 build_timeline(fps=vp["fps"]) 用的是真实值。
        fps = int(_vp["fps"])
        # NaN/inf 会一路传到 int(round(...)) 变成 traceback（ValueError/OverflowError），
        # 而本工具承诺坏输入只给一句话错误 + 退出码 2。min_dur 写成 NaN 时
        # max(min_dur, ...) 会把每段时长都变成 NaN，这是唯一能绕过下面 <=0 判断的值。
        if not math.isfinite(float(total)):
            _die("%s 估不出有效片长（total=%r）：检查 lead/tail/min_dur/hold 里是否写了 "
                 "NaN/inf 这类非有限数值" % (script_path, total), as_json)
        frames = int(round(total * fps))
        source = "script_estimate"
        source_note = ("粗估（等效字数 ÷ %.1f 字/秒 + lead/tail + hold；没有 %s）"
                       % (CPS, tl_path))
        if total <= 0:
            _die("%s 的 lines 里没有可估算的 text：每句都要有朗读文本" % script_path, as_json)

    jobs_req = int(args.jobs)
    if jobs_req < 1:
        _warn("--jobs %d 无效，按 1 算" % jobs_req)
        jobs_req = 1
    jobs_eff = max(1, min(jobs_req, CORES))
    if jobs_req > CORES:
        _warn("--jobs %d 超过本机 12 核：分块并行到 12 之后基本不再线性加速" % jobs_req)

    # 返工系数只接受有限正数：inf 会把 total_seconds 写成 JSON 里的 Infinity
    # （严格 JSON 消费方解析失败），NaN/负数会被 max() 悄悄夹成 0.1——用户拿到的是
    # "只渲 10% 时间"的排期，却没有任何提示。
    rerender = float(args.rerender)
    if not math.isfinite(rerender) or rerender <= 0:
        _warn("--rerender %r 无效（要有限正数，1.0 = 一遍过），按 1.0 算"
              % (args.rerender,))
        rerender = 1.0
    else:
        rerender = max(0.1, rerender)
    n = len(lines)
    sfx_cfg = script.get("sfx") or {}
    mus = script.get("music") or {}
    if not isinstance(sfx_cfg, dict):
        sfx_cfg = {}
    if not isinstance(mus, dict):
        mus = {}
    n_cues = len(sfx_cfg.get("cues") or [])
    sfx_on = bool(sfx_cfg.get("enabled", True)) and n_cues > 0
    # 配乐口径对齐 filmkit.music_plan：mode=off/none 或 enabled=false 才算关；
    # 只有 mode=score（或没写 mode 但写了 bpm）才会真跑配乐引擎（musicgen）。
    mode = str(mus.get("mode") or "").strip().lower()
    has_bpm = "bpm" in mus or "bpm" in script
    music_on = bool(mus.get("enabled", True)) and mode not in ("off", "none")
    score_on = music_on and (mode == "score" or (not mode and has_bpm))
    # crf 统一成整数再进报告：--json 的消费方按 int 解析，而剧本里写成字符串
    # （"17"）或脏值（"abc"）时旧实现会把原值透出成字符串，磁盘估算却按 17 算——
    # 同一份报告里 video.crf 与 disk 的口径对不上。
    try:
        crf = int(script.get("crf", 17))
    except (TypeError, ValueError):
        _warn("film.json 的 crf=%r 不是整数（render.py 会拒绝），磁盘按 17 估"
              % (script.get("crf"),))
        crf = 17
    preset = script.get("preset", "medium")

    # ---- 各阶段 ----
    # 没有实测的阶段一律给区间（正负 UNMEASURED_BAND），别把经验公式输出当精确值。
    def _band(v):
        # 下界永远让它摸到 0：这些阶段实测常常 <0.1 s（render.py 的 [time] 行按 0.1 s 打印，
        # 于是显示 0.0），下界写成正值会让"实测值落在区间外"这种假告警出现。
        return min(0.0, v * (1.0 - UNMEASURED_BAND)), v * (1.0 + UNMEASURED_BAND)
    workers = int(_num(script.get("tts_workers", 4), 4) or 4)
    workers = max(1, min(workers, max(1, n)))
    # TTS 按每句每 worker 摊：实测 4 句 / 4 workers = 2.2 s（串行口径 8.8 s）。
    tts_lo = n * TTS_SEC_PER_LINE[0] / float(workers)
    tts_hi = n * TTS_SEC_PER_LINE[1] / float(workers)
    layout_lo, layout_hi = _band(LAYOUT_BASE + LAYOUT_PER_LINE * n)
    cues_lo, cues_hi = _band(CUES_BASE + CUES_PER_LINE * n)
    if score_on:
        score_lo, score_hi = score_seconds(total)
    else:
        score_lo = score_hi = 0.0
    sfx_lo, sfx_hi = _band(max(SFX_MIN_SEC, n_cues * SFX_SEC_PER_CUE)) if sfx_on else (0.0, 0.0)
    audio_lo, audio_hi = _band(AUDIO_BASE + total * AUDIO_PER_SEC)
    fr_lo, fr_hi = frames_seconds(frames, jobs_eff, engine, rerender)
    mux_lo, mux_hi = mux_seconds_pair(frames, jobs_eff, total, script)
    qa_lo = max(0.5, QA_BASE_SEC[0] + total * QA_SEC_PER_SEC[0])
    qa_hi = max(qa_lo, QA_BASE_SEC[1] + total * QA_SEC_PER_SEC[1])
    # 三个可选门禁：预检（固定小开销）、全片 VMAF/SSIM、ASR 回读。
    # 实测口径（29.0 s 夹具片）：preflight 0.4 s；VMAF full 12–29 s（0.40–1.00× 片长，
    # 参考帧重渲与 libvmaf 都在里面，见 VMAF_* 常量注释）；ASR small ≈ 6–10 s。
    # 这三项全在**交付前**跑，不写进预演就等于"预演说 40 s、实际 90 s"——所以单独列 stage。
    qcfg = script.get("qa") or {}
    if not isinstance(qcfg, dict):
        qcfg = {}
    # render.py 的 _qa_vmaf_gate / _qa_asr_gate 都用 isinstance 兜住了非 dict 的配置块；
    # 预演以前直接 .get()，于是 "qa": {"vmaf": "off"} 这种（用户以为写了个开关）
    # 会在 --json 下甩 AttributeError traceback、退出码 1——比 render 还不宽容。
    vcfg = qcfg.get("vmaf")
    if not isinstance(vcfg, dict):
        if vcfg is not None:
            _warn("qa.vmaf 不是对象（收到 %r）：按默认 full 预演（render.py 会忽略这个值）"
                  % (vcfg,))
        vcfg = {}
    vmaf_mode = str(vcfg.get("mode") or "full").lower()
    vmaf_on = vmaf_mode != "off" and engine == "pil"      # web 路线没有可重渲参考帧
    # sample 模式（qavmaf 每 SAMPLE_STEP 帧取 1 帧）只渲/比 1/step 的帧，固定开销不变。
    # 预演以前不看 mode，于是 --vmaf sample 的真实成本被按全片算：29 s 片实测 sample
    # 2.95 s vs full 12.0 s，而预演两边都给同一个数（片长越长错得越多）。
    vmaf_step = 1
    if vmaf_mode == "sample":
        vmaf_step = _qavmaf_sample_step(vcfg)
    # sample 只压缩"逐帧"那一半，固定开销照付。
    vmaf_lo = (VMAF_BASE_SEC[0] + total * VMAF_SEC_PER_SEC[0] / vmaf_step) if vmaf_on else 0.0
    vmaf_hi = (VMAF_BASE_SEC[1] + total * VMAF_SEC_PER_SEC[1] / vmaf_step) if vmaf_on else 0.0
    acfg = qcfg.get("asr")
    if not isinstance(acfg, dict):
        if acfg is not None:
            _warn("qa.asr 不是对象（收到 %r）：按默认（开）预演" % (acfg,))
        acfg = {}
    asr_on = bool(acfg.get("enabled", True)) != False    # noqa: E712  默认开
    asr_lo = (total * 0.18 + 3.5) if asr_on else 0.0
    asr_hi = (total * 0.35 + 6.5) if asr_on else 0.0
    pre_lo, pre_hi = (0.3, 0.8)
    if not vmaf_on:
        vmaf_note = "本次不跑（--vmaf off 或 engine=web）"
    elif vmaf_mode == "sample":
        vmaf_note = ("sample 模式每 %d 帧取 1 帧：逐帧成本只算 1/%d，参考帧管线固定开销照付。"
                     "同机负载不同实测能差近 2×（SKILL.md §12：14.2 s / 另一轮 28.8 s）"
                     % (vmaf_step, vmaf_step))
    else:
        vmaf_note = ("全片逐帧比较（参考帧=场景函数重渲染，不是成片抽帧）。同机负载不同"
                     "实测能差近 2×（SKILL.md §12：14.2 s / 另一轮 28.8 s）")

    stages = [
        dict(stage="tts", lo=tts_lo, hi=tts_hi, measured=True,
             note=("%d 句 ÷ tts_workers=%d，每句每 worker %.1f–%.1f s（实测 2.2 s/4 句；"
                   "需联网，edge-tts 免费端点波动大）"
                   % (n, workers, TTS_SEC_PER_LINE[0], TTS_SEC_PER_LINE[1]))),
        dict(stage="layout", lo=layout_lo, hi=layout_hi, measured=False,
             note="排轴 + sub.srt/ass + 安全区/字形自查（经验模型，给 ±%d%% 区间）"
                  % int(UNMEASURED_BAND * 100)),
        dict(stage="cues", lo=cues_lo, hi=cues_hi, measured=False,
             note="%d 条 sfx.cues 落位 + 排序（经验模型，给 ±%d%% 区间）"
                  % (n_cues, int(UNMEASURED_BAND * 100))),
        dict(stage="score", lo=score_lo, hi=score_hi, measured=bool(score_on),
             note=("以实测点 %g s≈%.2g–%.2g s / %g s≈%.2g–%.2g s 为锚"
                   "（musicgen ≈31× 实时；--stage all 里与其它阶段争 CPU 会更慢；%g s 只是外推锚点）"
                   % (SCORE_REFS[0][0], SCORE_REFS[0][1], SCORE_REFS[0][2],
                      SCORE_REFS[1][0], SCORE_REFS[1][1], SCORE_REFS[1][2],
                      SCORE_REFS[2][0])
                   if score_on else
                   ("mode=%s：不跑配乐引擎（%s）"
                    % (mode or "-", "外部配乐/和弦床/关掉配乐")))),
        dict(stage="sfx", lo=sfx_lo, hi=sfx_hi, measured=False,
             note=("%d 条音效（实测 18 条 0.8 s；单条上限口径 ≤1.5 s，长环境声 wind/rain 另计）"
                   % n_cues if sfx_on else
                   "没有 sfx.cues：跳过（whoosh 兼容路径除外）")),
        dict(stage="audio", lo=audio_lo, hi=audio_hi, measured=False,
             note="三总线混音 + 写 audio.wav（实测 29 s 片 0.7 s；模型 %.2f + %.3f×片长）"
                  % (AUDIO_BASE, AUDIO_PER_SEC)),
        dict(stage="frames", lo=fr_lo, hi=fr_hi, measured=True,
             note=("%s：%s，--jobs %d → 有效加速比 %.2f×（实测 2 进程 1.51×），"
                   "返工系数 %.2g；不重编码时封装成本在 mux 行"
                   % (engine, _fps_label(engine), jobs_eff,
                      _speedup(jobs_eff), rerender))),
        dict(stage="mux", lo=mux_lo, hi=mux_hi, measured=True,
             note=("-c:v copy + 两遍 loudnorm（实测 29 s 片 1.7–1.9 s，与帧数无关）；"
                   "reencode_video=true 才走 x264 %.0f fps" % X264_FPS)),
        dict(stage="preflight", lo=pre_lo, hi=pre_hi, measured=True,
             note="渲染前资源预检（缺字形/坏音频/缺字体；实测 0.4 s，渲一帧之前就失败）"),
        dict(stage="qa", lo=qa_lo, hi=qa_hi, measured=True,
             note=("联系表 + 锐度参考帧重渲染 + 帧级深检（实测 29 s 片 6.2–6.8 s，不是 1 s）")),
        dict(stage="vmaf", lo=vmaf_lo, hi=vmaf_hi, measured=False, note=vmaf_note),
        dict(stage="asr", lo=asr_lo, hi=asr_hi, measured=False,
             note=("ASR 回读（faster-whisper small / CPU int8，RTF≈0.17 + 加载 2–3 s；"
                   "实测 29 s 片 6–10 s）"
                   if asr_on else "本次不跑（qa.asr.enabled=false）")),
    ]
    total_lo = sum(s["lo"] for s in stages)
    total_hi = sum(s["hi"] for s in stages)
    worst = max(stages, key=lambda s: s["hi"])
    share = worst["hi"] / total_hi if total_hi > 0 else 0.0

    # ---- 磁盘 ----
    disk = disk_estimate(frames, total, n, n_cues, crf, music_on)

    # ---- 分块（render.py 规则）----
    # 这里必须传**用户请求的 jobs**，不能传 jobs_eff：render.py 的 chunk_frames 公式用的是
    # --jobs 原值（它不按核数封顶），而 jobs_eff 是给"加速比模型"封顶到 CORES 的。
    # 传错的话 --jobs 16（24 线程机器上合法）会算出 chunk=120/8 段，render 实际是
    # chunk=60/15 段——预演与实现"逐字一致"的承诺当场失效。
    explicit = int(_num(script.get("chunk_frames"), 0))
    chunk, nseg = chunk_plan(frames, fps, jobs_req, explicit)
    chunk = dict(jobs=jobs_req, jobs_requested=jobs_req, chunk_frames=chunk,
                 segments=nseg, explicit=bool(explicit),
                 rule="chunk = chunk_frames 或 round(frames/jobs/fps/4)*4*fps，"
                      "再按 fps 取整、最小 2×fps")

    report = {
        "ok": True,
        "file": str(script_path),
        "source": source,
        "source_note": source_note,
        "video": {"title": script.get("title"),
                  # 内部构图恒为 1080p，交付尺寸可以更小（_vp 从 film.json 的 video 块来）
                  "width": _vp["w"], "height": _vp["h"],
                  "out_width": _vp["out_w"], "out_height": _vp["out_h"],
                  "fps": fps, "frames": frames,
                  "duration_s": round(total, 3), "engine": engine,
                  "crf": crf, "preset": preset, "bpm": mus.get("bpm", script.get("bpm")),
                  "style": script.get("style", None)},
        "counts": {"lines": n, "cues": n_cues, "tts_workers": workers},
        "jobs": {"requested": jobs_req, "effective": jobs_eff, "cores": CORES},
        "rerender": rerender,
        "stages": [dict(stage=s["stage"], label=STAGE_LABELS[s["stage"]],
                        seconds_lo=round(s["lo"], 2), seconds_hi=round(s["hi"], 2),
                        measured=s["measured"], note=s["note"]) for s in stages],
        "total_seconds": {"lo": round(total_lo, 2), "hi": round(total_hi, 2)},
        "disk": {"raw_peak_bytes": disk["raw_peak_bytes"],
                 "raw_peak_human": _fmt_bytes(disk["raw_peak_bytes"]),
                 "work_bytes": int(disk["work_bytes"]),
                 "work_human": _fmt_bytes(disk["work_bytes"]),
                 "out_bytes_lo": int(disk["out_bytes_lo"]),
                 "out_bytes_hi": int(disk["out_bytes_hi"]),
                 "out_human": "%s–%s" % (_fmt_bytes(disk["out_bytes_lo"]),
                                         _fmt_bytes(disk["out_bytes_hi"])),
                 "video_mbps": [round(v, 2) for v in disk["video_mbps"]]},
        "chunk": chunk,
        "bottleneck": {"stage": worst["stage"],
                       "seconds_hi": round(worst["hi"], 2),
                       "share": round(share, 3),
                       "advice": bottleneck_advice(worst["stage"], engine, jobs_eff)},
        "assumptions": [
            "内部构图恒为 1920x1080；交付尺寸/帧率可写 film.json 的 video 块"
            "（只允许 16:9 横屏且只能下采样；本次按 %d fps 估）" % fps,
            "lead=%.2f / tail=%.2f / min_dur=%.1f（body 基准；剧本写了就以剧本为准）"
            % (DEFAULT_LEAD, DEFAULT_TAIL, DEFAULT_MIN_DUR),
            "tts 耗时按 %d 句 ÷ workers × %.1f–%.1f s（每句每 worker）；实际受网络波动影响"
            % (n, TTS_SEC_PER_LINE[0], TTS_SEC_PER_LINE[1]),
            "frames 的并行不是 jobs 倍：有效加速比 = 1 + (jobs-1)×%.2f，封顶 %d 核"
            % (PARALLEL_EFF, CORES),
            "mux 默认 -c:v copy，只跟片长有关；reencode_video=true 才按帧数重编",
            "layout/cues/audio/sfx 给 ±%d%% 经验区间（无单阶段实测）" % int(UNMEASURED_BAND * 100),
            "可选门禁口径：preflight 0.4 s 固定；vmaf ≈ %.2f–%.2f× 片长 + %.1f–%.1f s"
            "（参考帧重渲染 + libvmaf；同机负载不同实测差近 2×）；asr ≈ 0.18–0.35× 片长 + "
            "3.5–6.5 s（faster-whisper small / CPU int8）"
            % (VMAF_SEC_PER_SEC[0], VMAF_SEC_PER_SEC[1], VMAF_BASE_SEC[0], VMAF_BASE_SEC[1]),
            "基准来源见 calibration 字段（机型/日期/样本）；换机请重测后回填顶部常量",
        ],
        "baseline": BASELINE_NOTE,
        "calibration": dict(CALIBRATION),
    }
    return report


def print_report(rep):
    """人读表格（--json 时不走这里，stdout 保持纯 JSON）。"""
    v = rep["video"]
    d = rep["disk"]
    ch = rep["chunk"]
    print("[estimate] %s" % rep["file"])
    if rep["source"] == "timeline":
        print("  来源: %s（已排轴：用真实 total/frames）" % rep["source_note"])
    else:
        print("  来源: %s" % rep["source_note"])
        print("        注意：这是**估算**，不是实测。跑过 --stage layout 后再跑本脚本，"
              "会自动改用真实帧数。")
    style = v.get("style")
    if style is None:
        # style 键已废弃且不生效：外观只由 work/design.json 决定。still 剧本里写了
        # 什么都当没写（filmcheck 会报「inert」警告），所以这里回一句能照着做的话，
        # 而不是旧版的「legacy 行为」——legacy 分支已经从 scenes.draw_frame 删掉了。
        style = "(没写；外观由 work/design.json 决定，先 propose --confirm)"
    print("  《%s》  片长 %.1f s / %d 帧 / %dx%d@%d / engine=%s / crf=%s preset=%s / style=%s"
          % (v.get("title") or "(未写 title)", v["duration_s"], v["frames"], v["width"], v["height"],
             v["fps"], v["engine"], v["crf"], v["preset"], style))
    print("  并行: --jobs %d（请求 %d；本机 12 核，>12 不再线性）  返工系数: %.2g"
          % (ch["jobs"], ch["jobs_requested"], rep["rerender"]))
    print()
    print("  %s %s %s" % (_pad("阶段", 8), _pad("耗时", 16), "说明"))
    for s in rep["stages"]:
        span = _fmt_span(s["seconds_lo"], s["seconds_hi"])
        tag = "" if s["measured"] else "[经验值] "
        print("  %s %s %s"
              % (_pad(s["stage"], 8), _pad(span, 16), tag + s["label"] + "：" + s["note"]))
    total = _fmt_span(rep["total_seconds"]["lo"], rep["total_seconds"]["hi"])
    print("  %s %s %s" % (_pad("合计", 8), _pad(total, 16), "--stage all 顺序跑完的墙钟（frames 并行）"))
    print()
    print("  磁盘")
    print("    raw 管道峰值   %s   %d×%d×3 字节 × %d 帧（原始帧流式喂 x264，不落盘，"
          "但决定内存/管道带宽）" % (_pad(d["raw_peak_human"], 10),
                                   _CANVAS[0], _CANVAS[1], v["frames"]))
    print("    work/          %s   segments + silent.mp4 + wav + tts + qa"
          "（分块并行时 segments 与 silent 会同时存在）" % _pad(d["work_human"], 10))
    print("    out/           %s   成片（视频 %.1f–%.1f Mbps + AAC 192 kbps）"
          % (_pad(d["out_human"], 10), d["video_mbps"][0], d["video_mbps"][1]))
    print()
    print("  分块建议（对齐 render.py：%s）" % ch["rule"])
    print("    --jobs %d" % ch["jobs"])
    print("    chunk_frames: %d   写进 film.json（render.py 没有 --chunk 参数，只从剧本读）"
          % ch["chunk_frames"])
    print("    预计分块 %d 段 × 约 %d 帧" % (ch["segments"], ch["chunk_frames"]))
    if ch["explicit"]:
        print("    （剧本里已显式写了 chunk_frames，这里按它算）")
    print()
    bn = rep["bottleneck"]
    print("  最大瓶颈：%s ≈ %.1f s（占 %.0f%%）—— %s"
          % (bn["stage"], bn["seconds_hi"], bn["share"] * 100, bn["advice"]))
    print()
    print("  基准说明：%s" % rep["baseline"])
    cal = rep.get("calibration") or {}
    if cal:
        print("  校准出处（换机请重测后回填 tools/estimate.py 顶部常量）：")
        print("    机型   %s" % cal.get("machine", "-"))
        print("    实测   %s · 样本 %s" % (cal.get("measured_at", "-"), cal.get("film", "-")))
        print("    方法   %s" % cal.get("method", "-"))
        obs = cal.get("observed") or {}
        if obs:
            print("    实测值 " + " · ".join("%s %.1fs" % (k, v) for k, v in obs.items()))
    print("  假设：")
    for a in rep["assumptions"]:
        print("    - %s" % a)


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(
        description="video-studio 成本/时长预演：渲多久、占多少盘、怎么分块")
    ap.add_argument("--script", default="film.json", help="剧本路径（默认 film.json）")
    ap.add_argument("--work", default="work", help="工作目录（有 timeline.json 就用真实帧数）")
    ap.add_argument("--jobs", type=int, default=4, help="frames 阶段并行进程数（默认 4，本机上限 12）")
    ap.add_argument("--engine", default="", choices=("", "pil", "web"),
                    help="覆盖 film.json 的 engine（空 = 按剧本，缺省 pil）")
    ap.add_argument("--rerender", type=float, default=1.0,
                    help="返工系数：画面预计重渲几遍（1.0 = 一遍过；1.4 = 预留 40%%）")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="机读输出（stdout 只有 JSON，键名英文）")
    args = ap.parse_args(argv)

    script_path, script = load_script(args.script, bool(args.as_json))
    rep = build_report(script_path, script, args)
    if args.as_json:
        sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
