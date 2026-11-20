# -*- coding: utf-8 -*-
"""footagekit.py — 素材剪辑生产线（footage 模式）的取材层。

这条线解决"有现成画面素材时怎么出片"：素材可以是网上下载的视频片段、
电影/纪录片镜头，也可以是一张张图片。它与"零素材代码生成"（PIL 逐帧画）并列，
两条线共用同一套底盘（TTS 实测时长、采样级混音、loudnorm、QA 报告）。

为什么要单独一层（而不是塞进 filmkit）：
  * filmkit 假设"每一帧都由 draw_frame 画出来"，所以它没有"源文件时间码"这个概念；
    素材线的全部复杂度都在"哪个源的第几秒"，这是另一套契约。
  * 素材线独有的坑（烧录字幕/台标水印/分段下载没有音轨/转写定位的编码问题）
    必须集中在一处，否则会散进每个工程的脚本里，每次都重踩。

本模块负责：契约读取 -> 素材体检 -> 画面治理建议 -> 下载/落地 -> 转写 -> 关键词定位。
剪辑（时间轴/EDL/切镜/拼接/混音/字幕）在 footageedit.py；命令行入口在 footage.py。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filmkit  # noqa: E402  （同目录同级模块：零拷贝，拷走 scripts/ 就能用）

SR = 48000
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
AUDIO_EXT = (".m4a", ".mp3", ".aac", ".wav", ".opus", ".flac")


def log(msg):
    """进度一律走 stderr，stdout 留给 --json 的机器可读输出。"""
    print(msg, file=sys.stderr, flush=True)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ============================================================================
# 1. 契约
# ============================================================================

def load_footage(path):
    """读 footage.json（或直接给 dict）。

    这里只拦"结构上的硬错误"（缺字段 / id 重复 / shot 指向不存在的 source）。
    字段级与合规级校验交给 footageqa.py 的 F 门禁——"能跑"和"合格"必须分开，
    否则一个坏配置要么静默跑通、要么在渲染中途炸。
    """
    if isinstance(path, dict):
        d = dict(path)
        base = Path.cwd()
    else:
        p = Path(path)
        if not p.is_file():
            raise SystemExit("找不到 footage.json：%s" % p)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit("footage.json 不是合法 JSON：%s" % e) from None
        base = p.parent
    if not isinstance(d, dict):
        raise SystemExit("footage.json 顶层必须是对象")
    if not str(d.get("title") or "").strip():
        raise SystemExit("footage.json 缺 title")
    lines = d.get("lines")
    if not isinstance(lines, list) or not lines:
        raise SystemExit("footage.json 的 lines 必须是非空数组")
    seen = set()
    for ln in lines:
        if not isinstance(ln, dict):
            raise SystemExit("lines[] 里有一项不是对象")
        lid = str(ln.get("id") or "").strip()
        if not lid:
            raise SystemExit("lines[] 有一项缺 id")
        if lid in seen:
            raise SystemExit("lines[] 的 id 重复：%s" % lid)
        seen.add(lid)
        if not str(ln.get("text") or "").strip():
            raise SystemExit("lines[%s] 缺 text（朗读文本）" % lid)
    srcs = d.get("sources")
    if not isinstance(srcs, list) or not srcs:
        raise SystemExit("footage.json 的 sources 必须是非空数组（至少一个素材源）")
    ids = set()
    for s in srcs:
        sid = str(s.get("id") or "").strip()
        if not sid:
            raise SystemExit("sources[] 有一项缺 id")
        if sid in ids:
            raise SystemExit("sources[] 的 id 重复：%s" % sid)
        ids.add(sid)
        if not (s.get("path") or s.get("url")):
            raise SystemExit("sources[%s] 必须给 path（本地文件）或 url（远程）" % sid)
    for ln in lines:
        shot = ln.get("shot")
        if not shot:
            continue
        if not isinstance(shot, dict):
            raise SystemExit("lines[%s].shot 必须是对象" % ln.get("id"))
        if str(shot.get("source") or "") not in ids:
            raise SystemExit("lines[%s].shot.source=%r 不在 sources 里"
                             % (ln.get("id"), shot.get("source")))
    d["_base"] = str(base)
    return d


def source_map(footage):
    return {str(s["id"]): s for s in footage["sources"]}


def source_path(footage, src):
    """素材源的本地路径：给了 path 用它，否则按 url 的文件名落到 dl/ 下。"""
    base = Path(footage.get("_base") or Path.cwd())
    if src.get("path"):
        p = Path(str(src["path"]))
        return p if p.is_absolute() else (base / p)
    url = str(src.get("url") or "")
    name = Path(urllib.parse.urlparse(url).path).name or (str(src.get("id")) + ".bin")
    return base / "dl" / name


# ============================================================================
# 2. probe：素材体检
# ============================================================================

def probe_file(path):
    """素材体检：时长 / 视频流 / 音频流 / 有无音轨 / 是否图片。

    返回的 has_audio 很关键：视频站的分段下载（--download-sections）常常**只给视频流**，
    等到混音阶段才发现"原声是空的"，那时已经切了几十个镜头。所以在探针阶段就报出来。
    """
    p = Path(path)
    if not p.exists():
        raise SystemExit("素材不存在：%s" % p)
    raw = filmkit.probe(str(p)) or {}
    fmt = raw.get("format") or {}
    dur = float(fmt.get("duration") or 0.0)
    vs, aus = [], []
    for s in raw.get("streams") or []:
        if s.get("codec_type") == "video":
            fps = str(s.get("r_frame_rate") or "0/1")
            try:
                num, den = fps.split("/")
                fps_v = round(float(num) / float(den), 3) if float(den) else 0.0
            except Exception:
                fps_v = 0.0
            vs.append({"codec": s.get("codec_name"), "w": int(s.get("width") or 0),
                       "h": int(s.get("height") or 0), "fps": fps_v})
        elif s.get("codec_type") == "audio":
            aus.append({"codec": s.get("codec_name"), "sr": s.get("sample_rate"),
                        "channels": s.get("channels")})
    return {"path": str(p), "dur": round(dur, 3), "video": vs, "audio": aus,
            "has_audio": bool(aus), "is_image": p.suffix.lower() in IMAGE_EXT,
            "is_audio_only": bool(aus) and not vs,
            "size": int(fmt.get("size") or 0)}


def probe_all(footage):
    """批量体检 sources，写 work/sources.json。"""
    out = {}
    for sid, src in source_map(footage).items():
        p = source_path(footage, src)
        if not p.exists():
            out[sid] = {"path": str(p), "missing": True}
            log("  [missing] %s -> %s" % (sid, p))
            continue
        info = probe_file(p)
        out[sid] = info
        log("  %-12s %7.1fs  %s  audio=%s  %s" % (
            sid, info["dur"],
            ("%dx%d@%g" % (info["video"][0]["w"], info["video"][0]["h"], info["video"][0]["fps"]))
            if info["video"] else "audio-only",
            "yes" if info["has_audio"] else "NO",
            p.name))
    return out


# ============================================================================
# 3. 画面治理（treatment）
# ============================================================================

def suggest_treatment(info):
    """给一份保守的治理建议：底部 12% 当字幕带、右上 20%x8% 当水印区。

    这只是**建议**，不自动生效：写进 footage.json 的 treatment 才算数。
    为什么不自动套：有些片的字幕在画面正中（舞台剧/访谈），
    自动裁底部反而把字幕切成两半——治理参数必须由人看一帧再定。
    """
    if not info.get("video"):
        return {"crop": None, "reason": "没有视频流，无需治理"}
    w = int(info["video"][0]["w"])
    h = int(info["video"][0]["h"])
    sub_top = int(h * 0.88)
    cut_bottom = h - sub_top
    cut_top = int(h * 0.08)
    keep_h = h - cut_top - cut_bottom
    keep_h -= keep_h % 2
    wm = [int(w * 0.80), 0, w - int(w * 0.80), int(h * 0.08)]
    return {"crop": "%d:%d:0:%d" % (w, keep_h, cut_top),
            "reason": "建议裁掉顶部 %dpx（水印带）与底部 %dpx（字幕带），保留 %dx%d 画面区"
                      % (cut_top, cut_bottom, w, keep_h),
            # 这两个声明值是要**粘进 footage.json** 的：F2/F3 用它们做几何判定，
            # 不依赖"画面看起来像不像有字幕"这种不可靠的图像指标。
            "burned_subs": {"y0": sub_top, "y1": h},
            "watermark": {"x": wm[0], "y": wm[1], "w": wm[2], "h": wm[3]},
            "assumed_sub_band": [0, sub_top, w, cut_bottom]}


def build_treatment_filter(treatment, video):
    """把 treatment 编译成 ffmpeg 滤镜串（切镜与抽帧共用同一份，避免"预览和成片不一样"）。

    treatment 字段（都可选，按顺序应用）：
      crop      "w:h:x:y"          先裁（去字幕/水印/去黑边）
      scale     "w:h"              再缩放（默认等比缩到输出宽）
      letterbox "w:h:x:y:color"    补遮幅（默认 pad 到输出画布，颜色取 letterbox_color）
      color     "eq=..."           最后调色（统一不同素材的色差）
      fps       数字               帧率（默认取 video.fps）
    """
    vw = int(video.get("width") or 1920)
    vh = int(video.get("height") or 1080)
    fps = video.get("fps") or 30
    chain = []
    crop = str(treatment.get("crop") or "").strip()
    if crop:
        chain.append("crop=" + crop)
    scale = str(treatment.get("scale") or "").strip()
    if not scale:
        # 没有显式 scale 时：按宽度缩放到输出画布宽，高度由宽高比决定（不拉伸）
        cw = int(crop.split(":")[0]) if crop else vw
        chh = int(crop.split(":")[1]) if crop else vh
        if cw and chh:
            # 只按宽度等比缩，高度由 crop 的宽高比反推（不拉伸）。
            scale = "%d:%d" % (vw, max(2, int(round(vw * chh / cw))))
    if scale:
        sw, sh = [int(x) for x in scale.split(":")[:2]]
        if sh > vh:      # 超高就按高度贴边，避免 pad 失败
            sw = max(2, int(round(sw * vh / float(sh))))
            sh = vh
        if sw % 2:
            sw += 1
        if sh % 2:
            sh += 1
        chain.append("scale=%d:%d:flags=lanczos" % (sw, sh))
        pad_y = max(0, (vh - sh) // 2)
        color = str(treatment.get("letterbox_color") or "0x0b0c0e")
        chain.append("pad=%d:%d:0:%d:color=%s" % (vw, vh, pad_y, color))
    color_eq = str(treatment.get("color") or "").strip()
    if color_eq:
        chain.append(color_eq)
    chain.append("fps=%s" % fps)
    chain.append("setsar=1")
    chain.append("format=yuv420p")
    return ",".join(chain)


# ============================================================================
# 4. fetch：下载/落地
# ============================================================================

def have_ytdlp():
    """检测 yt-dlp（外部命令）。返回可用的调用前缀，或 None。

    为什么不写成 Python 依赖：视频站的签名与风控变化快，只有持续更新的 yt-dlp 跟得上；
    把它当外部命令，技能本体就仍然只有 stdlib + numpy/PIL，缺它也只是"这一路走不了"，
    本地文件与直链两条路照常可用（这两条才是可复现的那两条）。
    """
    for cmd in (["yt-dlp", "--version"], [sys.executable, "-m", "yt_dlp", "--version"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if p.returncode == 0 and (p.stdout or "").strip():
                return " ".join(cmd)
        except Exception:
            continue
    return None


def _http_download(url, dst, *, referer=None, user_agent=None, timeout=180):
    """流式下载：先写 .part，成功后原子替换（断网不会把已有好文件截成 0 字节）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", user_agent or
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    if referer:
        req.add_header("Referer", referer)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dst)
    return dst


def download(footage, src, *, force=False, format_sel=None):
    """把一个素材源落地成本地文件，返回 {path, skipped, bytes}。

    三条路径（按可复现性排序）：
      1. 本地 path —— 直接可用，离线可复现（推荐用于回归样例）
      2. 直链 —— urllib 流式下载
      3. 站点页面 —— yt-dlp（支持 sections 只下时间窗、format_sel 选清晰度）
    """
    dst = source_path(footage, src)
    if dst.exists() and dst.stat().st_size > 1024 and not force:
        log("  [skip] %s 已存在（%.1f MB）" % (dst.name, dst.stat().st_size / 1048576.0))
        return {"path": str(dst), "skipped": True, "bytes": dst.stat().st_size}
    if src.get("path"):
        raise SystemExit("sources[%s].path 指向的文件不存在：%s" % (src.get("id"), dst))
    url = str(src.get("url") or "")
    if not url:
        raise SystemExit("sources[%s] 既没有本地文件也没有 url" % src.get("id"))
    headers = src.get("headers") or {}
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    kind = str(src.get("kind") or "").lower()
    if ext in (VIDEO_EXT + AUDIO_EXT + IMAGE_EXT) and not src.get("sections"):
        log("  [http] %s -> %s" % (url[:120], dst.name))
        _http_download(url, dst, referer=headers.get("Referer"),
                       user_agent=headers.get("User-Agent"))
        return {"path": str(dst), "skipped": False, "bytes": dst.stat().st_size}
    ytdlp = have_ytdlp()
    if not ytdlp:
        raise SystemExit(
            "该源需要 yt-dlp（非直链），本机没有：\n"
            "  安装：python -m pip install -U yt-dlp\n"
            "  或自己下好文件，把 sources[%s].path 指向本地文件（推荐：可复现、可离线）\n"
            % src.get("id"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    stem = dst.with_suffix("")
    args = ytdlp.split() + ["--no-playlist", "--no-warnings",
                            "-o", str(stem) + ".%(ext)s"]
    for a, b in (src.get("sections") or []):
        args += ["--download-sections", "*%ss-%ss" % (a, b)]
    fs = format_sel or src.get("format_sel")
    if not fs:
        fs = "ba" if kind == "audio" else "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/bv*+ba"
    args += ["-f", fs]
    if headers.get("Referer"):
        args += ["--referer", headers["Referer"]]
    if headers.get("User-Agent"):
        args += ["--user-agent", headers["User-Agent"]]
    args.append(url)
    log("  [yt-dlp] %s ..." % " ".join(args[:6]))
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("yt-dlp 失败（exit=%d）：\n%s"
                         % (p.returncode, (p.stderr or "")[-1500:]))
    cand = [c for c in dst.parent.glob(stem.name + ".*")
            if c.suffix.lower() not in (".part", ".ytdl")]
    if not cand:
        raise SystemExit("yt-dlp 报成功但找不到产物：%s.*" % stem)
    best = max(cand, key=lambda q: q.stat().st_size)
    return {"path": str(best), "skipped": False, "bytes": best.stat().st_size}



# ============================================================================
# 素材预算：按片长反算"至少要有几个不同素材源"
# ============================================================================
# 为什么需要它：素材线最容易偷懒的地方是"一路指同一个源"——40 个镜头全来自
# 2 张图，片子里每一秒都是同一张画在平移。QA 的 F1..F7 一条都拦不住它
# （EDL 不越界、没有字幕残留、音画等长、有出处），交付时只能靠人肉发现。
# 实测反例：一部 6 分 25 秒的片子用了 2 个来源，门禁全绿。
#
# 口径（用户拍板）：
#   * 推荐密度 3 个独立来源 / 分钟 -> 低于它 pass + warning（画面重复度偏高）
#   * 硬下限 2 个 / 分钟           -> 低于它 fail（不许静默放过）
#   * 再短的片子也不许少于 3 个来源（否则就是"一张图 + 字幕"）
SOURCE_PER_MIN_RECOMMENDED = 3
SOURCE_PER_MIN_MINIMUM = 2
SOURCE_ABS_MINIMUM = 3


def source_budget(total_sec, *, per_min_recommended=SOURCE_PER_MIN_RECOMMENDED,
                  per_min_minimum=SOURCE_PER_MIN_MINIMUM,
                  absolute_minimum=SOURCE_ABS_MINIMUM):
    """按片长算素材预算：{recommended: 推荐数, minimum: 硬下限, total_sec, per_min}。

    推荐值与硬下限**分开算**：推荐 3/min 是"画面不重复"的舒服线，硬下限 2/min
    是"明显偷懒"的红线。只留一个数的话，要么放过 2 张图，要么把正常片拦死。
    """
    import math
    t = max(0.0, float(total_sec or 0.0))
    minutes = t / 60.0
    recommended = max(absolute_minimum, int(math.ceil(minutes * per_min_recommended - 1e-9)))
    minimum = max(absolute_minimum, int(math.ceil(minutes * per_min_minimum - 1e-9)))
    minimum = min(minimum, recommended)
    return {"total_sec": round(t, 3), "per_min": per_min_recommended,
            "recommended": recommended, "minimum": minimum,
            "fresh_shot_sec": round(t / max(1, recommended), 2)}


def source_usage(edl):
    """EDL 的素材使用情况：distinct 源数 / 每源镜头数 / 最长连续同源 / 重复取景窗。

    只看 shot.source，不看文件名——同一张图被切成多个视频片段仍算**一个**来源，
    这正是"素材量不足"最容易被伪装的地方（把一张图切成 40 段也算 40 个镜头）。
    """
    shots = list((edl or {}).get("shots") or [])
    per_src, max_run, run_src, run = {}, 0, None, 0
    for s in shots:
        sid = str(s.get("source") or "")
        per_src[sid] = per_src.get(sid, 0) + 1
        if sid == run_src:
            run += 1
        else:
            run_src, run = sid, 1
        max_run = max(max_run, run)
    # 重复取景窗：同源且 at 相差 < 0.5s，说明是同一个画面被反复用
    dupes = 0
    by_src = {}
    for s in shots:
        by_src.setdefault(str(s.get("source") or ""), []).append(float(s.get("at") or 0.0))
    for _sid, ats in by_src.items():
        ats.sort()
        for a, b in zip(ats, ats[1:]):
            if b - a < 0.5:
                dupes += 1
    return {"shots": len(shots), "distinct": len(per_src),
            "per_source": per_src, "max_consecutive": max_run,
            "duplicate_windows": dupes}

def fetch_all(footage, *, force=False):
    out = {}
    for sid, src in source_map(footage).items():
        log("== fetch %s" % sid)
        out[sid] = download(footage, src, force=force)
    return out


# ============================================================================
# 5. transcribe：转写（素材线的"字幕轨"，也是定位镜头的唯一可靠依据）
# ============================================================================

def _decode_pcm(path, sr=16000):
    """ffmpeg 解成 float32 单声道 PCM。

    为什么不用 faster_whisper 自带的 decode_audio：它依赖 PyAV，而 PyAV 与
    faster_whisper 的版本组合经常对不上（实测 av.open(metadata_errors=...) 直接 TypeError）。
    自己用 ffmpeg 解一次只占极小成本，却把"环境一不匹配整条门禁就崩"的坑绕开。
    """
    p = subprocess.run([filmkit.FFMPEG, "-v", "error", "-i", str(path), "-vn",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise SystemExit("解码失败（%s）：%s"
                         % (path, p.stderr.decode("utf-8", "replace")[-800:]))
    import numpy as np
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def _cache_key(items):
    h = hashlib.sha1()
    for it in items:
        h.update(str(it).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def transcribe(media, work, *, model="small", lang="zh", force=False,
               allow_missing=False, source_id=None):
    """转写音轨/视频 -> work/transcripts/<key>.json，带内容指纹缓存。

    缓存键 = 文件名 + 大小 + mtime + 模型 + 语种。改素材或换模型都不会复用旧转写
    （"文件存在就用"这种缓存是会骗人的，TTS 那边踩过一次）。
    """
    media = Path(media)
    if not media.exists():
        raise SystemExit("要转写的文件不存在：%s" % media)
    st = media.stat()
    key = _cache_key([media.name, st.st_size, int(st.st_mtime), model, lang])
    out = Path(work) / "transcripts" / (key + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        # 只在"真的转写过"时复用。以前 enabled:false 的跳过记录也走同一路径缓存，
        # 于是第一次用 --allow-missing-asr 跳过之后，装了 faster-whisper 也不会重转——
        # 用户以为转写在跑，其实一直在复用一条"没转"的记录。
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("enabled") is not False:
            log("  [skip] 复用转写 %s" % out.name)
            return cached
        log("  [asr] 旧缓存是跳过记录（enabled=false），本次重新转写")
    try:
        import faster_whisper  # noqa: F401
        has_fw = True
    except Exception:
        has_fw = False
    if not has_fw:
        if not allow_missing:
            raise SystemExit(
                "没装 faster-whisper，无法转写（素材线靠它定位镜头）：\n"
                "  python -m pip install -U faster-whisper\n"
                "  已经手填了 lines[].shot.at/len 的话，加 --allow-missing-asr 跳过转写")
        log("  [WARN] 没装 faster-whisper：跳过转写（定位退化为手填时间码）")
        data = {"schema": 1, "enabled": False, "segs": [], "dur": 0.0,
                "model": model, "lang": lang, "src": str(media),
                "skipped": "faster-whisper 不可用", "created": _now()}
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data
    t0 = time.time()
    log("  [asr] 解码 %s" % media.name)
    # 采样率只写一次：_decode_pcm 的默认 sr 与这里算时长用的除数必须是同一个常量，
    # 否则改 sr 参数后时长会静默算错（16k 是 ASR 侧口径，与音频总线的 48k 无关）。
    asr_sr = 16000
    audio = _decode_pcm(media, sr=asr_sr)
    dur = len(audio) / float(asr_sr)
    log("  [asr] %.1fs 音频，加载模型 %s" % (dur, model))
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")
    it, info = m.transcribe(audio, language=(lang or None), vad_filter=True,
                            beam_size=1, condition_on_previous_text=False)
    segs = []
    for s in it:
        segs.append({"a": round(float(s.start), 2), "b": round(float(s.end), 2),
                     "t": (s.text or "").strip()})
        if len(segs) % 200 == 0:
            log("  [asr] %d 句 @ %.0fs" % (len(segs), time.time() - t0))
    data = {"schema": 1, "enabled": True, "src": str(media), "source_id": source_id,
            "dur": round(dur, 2), "model": model, "lang": lang,
            "lang_detected": getattr(info, "language", None),
            "cost_s": round(time.time() - t0, 1), "created": _now(), "segs": segs}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("  [asr] %d 句 -> %s（%.0fs）" % (len(segs), out.name, data["cost_s"]))
    return data


def merge_segments(segs, gap=1.6, max_chars=120):
    """把逐句合并成"阅读行"：间隔 < gap 且不超长的接在一起，便于人读与检索。"""
    out, cur = [], None
    for s in segs:
        t = str(s.get("t") or "").strip()
        if not t:
            continue
        if cur is None or s["a"] - cur["b"] > gap or len(cur["t"]) >= max_chars:
            if cur:
                out.append(cur)
            cur = {"a": s["a"], "b": s["b"], "t": t}
        else:
            cur["b"] = s["b"]
            cur["t"] = cur["t"] + t
    if cur:
        out.append(cur)
    return out


# ============================================================================
# 6. locate：关键词定位（内容 -> 时间码）
# ============================================================================

def locate(transcript, keywords, *, n=12, min_dur=0.0, max_dur=60.0, window=None):
    """在转写里按关键词找候选时间码，按命中数排序。

    中文关键词**不要走 shell 命令行参数**：Windows 控制台是 GBK，
    python x.py "茅台" 传进来的字符串已经是乱码，在 UTF-8 文本里永远 0 命中。
    这个坑在一次真实出片里直接导致误判"转写里没有这个词"。所以定位只走本函数，
    或把关键词写进 JSON 再读。

    window=(a, b) 先把范围收敛到某段时间——比堆关键词有效得多
    （地名容易转错，"茅台/毛台"、"娄山关/楼山关"混着出现）。
    """
    segs = transcript.get("segs") or []
    hits = []
    for i, s in enumerate(segs):
        dur = float(s["b"]) - float(s["a"])
        if dur < min_dur or dur > max_dur:
            continue
        if window and (float(s["b"]) < window[0] or float(s["a"]) > window[1]):
            continue
        text = str(s.get("t") or "")
        matched = [k for k in keywords if k and k in text]
        if not matched:
            continue
        ctx = "".join(str(segs[j].get("t") or "")
                      for j in range(max(0, i - 1), min(len(segs), i + 2)))
        hits.append({"a": s["a"], "b": s["b"], "text": text, "score": len(matched),
                     "matched": matched, "context": ctx[:160]})
    hits.sort(key=lambda h: (-h["score"], h["a"]))
    return hits[:n]


def locate_report(hits, keywords=None):
    """把候选排成 UTF-8 文本（控制台乱码，所以由调用方写文件或直接进 --json）。"""
    lines = []
    if keywords:
        lines.append("关键词: %s" % " / ".join(keywords))
    if not hits:
        lines.append("（无命中。先确认转写是否覆盖该时间窗；中文关键词不要走 shell 参数）")
        return "\n".join(lines)
    for h in hits:
        lines.append("%8.2f-%8.2f  score=%d  %s" % (h["a"], h["b"], h["score"], h["text"][:60]))
    return "\n".join(lines)
