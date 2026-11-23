# -*- coding: utf-8 -*-
"""compose.py — 「素材打底 + 小面积叠加」的合成入口（hybrid 的第三种形态）。

为什么要有它
------------
hybrid（素材 + 自绘结合）在技能里此前只有一种做法：**按段拼接**（素材段走 footage.py、
图文段走 render.py、最后 concat）。实测还有两种更省的形态，其中第三种最好用：

  1. 按段拼接       —— 图文段整段独立成片；风格断层靠交叉淡化/暗场过渡掩盖
  2. 整屏卡片覆盖   —— 在素材上盖满屏卡片；素材被遮掉，等于没用到素材
  3. **素材打底 + 小面积叠加**（本脚本）—— 素材就是画面，只把"必要的一点信息"
     （一行对照、一个标注、一个角标）按时间窗叠上去

本脚本只做第 3 种，并且强制两件事（都是实测踩过的坑）：
  · **帧对齐**：镜头按累计帧切（footageedit.cut_shots 已帧对齐），拼接后视频帧数
    必须等于 round(total*fps)；不等就报错，不允许"差半秒也能出片"。
  · **响度归一化不可绕过**：内部固定走 filmkit.loudnorm_two_pass。
    绕过它手写合成会得到 -25 LUFS 的成片（实测发生过），而且容器/帧级门禁都查不出来。

契约：overlay.json
------------------
{
  "canvas": [1920, 1080],
  "subtitle": "sub.ass",
  "layers": [
    {"id": "s04", "png": "chips/s04.png", "bind": "line:s04",
     "x": "(W-w)/2", "y": 692, "fade": 0.18},
    {"id": "corner", "png": "chips/logo.png", "at": 12.0, "dur": 4.0,
     "x": "W-w-48", "y": "48"}
  ]
}

时间窗解析优先级：bind > (at + dur) > (at，到下一句起点)。
坐标：整数或 ffmpeg 表达式；x/y 支持 (W-w)/2、W-w-48 这类写法（W/w 是主/叠加宽）。

用法
----
    python compose.py --footage footage.json --overlay overlay.json --work work --out out/film.mp4
    python compose.py ... --stage overlay|silent|audio|compose|qa
    python compose.py ... --target-tp -2.2 --crf 22

退出码：0 成功 / 1 缺输入或 ffmpeg 失败 / 2 契约错误（overlay.json 不合法）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import filmkit as K          # noqa: E402

STAGES = ("overlay", "silent", "audio", "compose", "qa")
DEFAULT_TARGET_TP = -2.2     # AAC 会抬高真峰 0.5~1.1 dB，见 filmkit.loudnorm_two_pass


def _utf8_stdout():
    """Windows 控制台默认 GBK：打印中文/子进程回显会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                 # noqa: BLE001 —— 老 Python / 非 tty 直接跳过
            pass


def log(msg):
    try:
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:            # 兜底：宁可掉字符也不能中断出片
        sys.stdout.write(str(msg).encode("utf-8", "replace").decode("gbk", "replace") + "\n")
        sys.stdout.flush()


def _load_json(path, default=None):
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(str(tmp), str(p))


def media_dur(path):
    """读时长；读不到返回 0.0（调用方必须自己判 0，不能当成功）。"""
    try:
        return float(K.media_dur(str(path)) or 0.0)
    except Exception:                     # noqa: BLE001
        return 0.0


def _run(cmd, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", cwd=cwd)
    if p.returncode != 0:
        raise SystemExit("[compose] ffmpeg 失败（exit=%d）：\n%s\n%s"
                         % (p.returncode, " ".join(str(c) for c in cmd[:12]),
                            (p.stderr or "")[-2000:]))
    return p


# ============================================================================
# 一、契约校验
# ============================================================================

def load_overlay(path):
    """读并校验 overlay.json。字段不合法直接 SystemExit（退出码 2 语义）。"""
    spec = _load_json(path)
    if not isinstance(spec, dict):
        log("[compose] overlay.json 读不到或不是对象：%s" % path)
        raise SystemExit(2)
    layers = spec.get("layers") or []
    if not isinstance(layers, list):
        log("[compose] overlay.layers 必须是数组")
        raise SystemExit(2)
    bad = []
    for i, ly in enumerate(layers):
        if not isinstance(ly, dict):
            bad.append("layers[%d] 不是对象" % i); continue
        if not ly.get("png"):
            bad.append("layers[%d] 缺 png（叠加层的透明图路径）" % i)
        bind = ly.get("bind")
        at_v = ly.get("at")
        if bind is not None and not isinstance(bind, str):
            bad.append("layers[%d].bind 必须是字符串（形如 line:<句 id>）" % i)
        for _key, _val in (("at", at_v), ("dur", ly.get("dur"))):
            if _val is not None and (isinstance(_val, bool)
                                     or not isinstance(_val, (int, float))):
                bad.append("layers[%d].%s 必须是数字（秒），收到 %r" % (i, _key, _val))
        # at=0.0 是合法时间窗（片子第一帧就出现），不能按真假值判成"缺时间窗"。
        if not bind and at_v is None:
            bad.append("layers[%d] 缺时间窗：给 bind（推荐）或 at" % i)
        fade = ly.get("fade")
        if fade is not None and not isinstance(fade, (int, float)):
            bad.append("layers[%d].fade 必须是数字（秒）" % i)
    if bad:
        for b in bad:
            log("  [契约错误] " + b)
        log("[compose] overlay.json 不合法（见上）")
        raise SystemExit(2)
    spec["layers"] = layers
    return spec


def resolve_windows(spec, tl):
    """把每层的时间窗算成 (start, end) 秒。返回 [{id,png,x,y,fade,start,end}]。"""
    rows = {str(r["id"]): r for r in (tl.get("lines") or [])}
    order = [str(r["id"]) for r in (tl.get("lines") or [])]
    total = float(tl.get("total") or 0.0)
    out = []
    for li, ly in enumerate(spec.get("layers") or []):
        bind = str(ly.get("bind") or "")
        if bind.startswith("line:"):
            lid = bind.split(":", 1)[1]
            r = rows.get(lid)
            if r is None:
                raise SystemExit("[compose] bind=%r 在 timeline 里找不到该句" % bind)
            start = float(r["start"])
            end = float(r["end"])
            i = order.index(lid) if lid in order else -1
            if ly.get("until") == "next" and 0 <= i < len(order) - 1:
                end = float(rows[order[i + 1]]["start"])
        elif ly.get("at") is not None:
            start = float(ly["at"])
            if ly.get("dur"):
                end = start + float(ly["dur"])
            else:
                nxt = [float(rows[k]["start"]) for k in order
                       if float(rows[k]["start"]) > start + 1e-6]
                end = min(nxt) if nxt else total
        else:
            # 写了 bind 但不是 "line:<id>"（例如写成 shot:s02）时，这一层既不出现在
            # --stage overlay 的窗口表里、也不进成片，而且没有任何报错——契约错误必须
            # 显式失败（docstring：退出码 2 = overlay.json 不合法）。
            log("  [契约错误] layers[%d] 的时间窗无法解析：bind=%r 只认 'line:<句 id>'；"
                "改用 at（秒）也行" % (li, ly.get("bind")))
            raise SystemExit(2)
        start = max(0.0, start + float(ly.get("pad_start") or 0.0))
        end = min(total or end, end + float(ly.get("pad_end") or 0.0))
        if end - start <= 0.05:
            log("  [skip] %s 的时间窗过短（%.2f~%.2f）" % (ly.get("id") or "?", start, end))
            continue
        out.append({"id": str(ly.get("id") or Path(str(ly["png"])).stem),
                    "png": str(ly["png"]),
                    "x": str(ly["x"] if ly.get("x") is not None else "(W-w)/2"),
                    "y": str(ly["y"] if ly.get("y") is not None else "H-h-120"),
                    "fade": float(ly.get("fade") or 0.0),
                    "role": str(ly.get("role") or "chip"),
                    "start": round(start, 3), "end": round(end, 3)})
    return out


# ============================================================================
# 叠加预算审计：形态 3 的红线是"素材还是画面"
# ============================================================================
# 第 3 种形态（素材打底 + 小面积叠加）一旦叠多了就退化成"整屏卡片"，观众看到
# 的不再是素材。SKILL.md 写的是"自绘 <=15%"，但此前**没有任何代码量它**：
# 实测一部 5 分半的片子叠了 43 层题头带，累计覆盖约 20%，门禁全绿。这里把它变成可执行判据：
#   * 单层覆盖面积（图层 PNG 的不透明像素占全画面比例）
#   * 每句的叠加层数（>=3 层 = 回到卡片）
#   * 被叠加覆盖的句数占比（>=95% = 全片都在叠）
#   * 时长加权覆盖（>60% 时长画面被叠）
# 阈值刻意分两档：WARN 是"该看一眼"，FAIL 是"这就不是素材为主的片子了"。
# 18.7% 是"一行对照/一条题头带"的常态面积（拿真片量出来的），阈值定 20% 才不会
# 把正常设计全部标成 warning；30% 的 fail 线才是"整屏卡片"的红线。
OVL_AREA_WARN = 0.20
OVL_AREA_FAIL = 0.30
# 封面/章节卡（role=cover）是**故意**整屏的：开场与转场各一张是形态 3 的正常表达。
# 但它们必须显式声明且限量——否则"每句都盖一张"就能靠 role 混过去。
OVL_COVER_AREA_MAX = 0.65
OVL_COVER_MAX = 4
# 小面积标注（一行对照、一个字卡）不算"覆盖句子"：它们不遮素材主体。
# 这条是拿真片校准出来的——5 分半那版每句都有一张小卡，但面积只有 1.5%~6%，
# 观感上素材仍然是画面；只按句子数算会把它误判成"全片都在叠"。
OVL_COUNT_AREA = 0.06
OVL_LAYERS_PER_LINE_FAIL = 3
OVL_LINES_WARN = 0.80
OVL_LINES_FAIL = 0.95
OVL_TIME_WARN = 0.60


def layer_coverage(png_path, canvas=None):
    """图层覆盖**画布**的比例（0..1）。缺 PIL 时返回 None（由调用方记 skipped）。

    按画布面积归一，不是按 PNG 自身面积：小卡片（760x150 的对照条）自身像素
    几乎全不透明，若按自身面积算就是 99.9%，会把它误判成"整屏卡片"。
    实测：5 分半那版 43 层信息层逐层 1.5%~19%，按画布算才读得出"素材仍是画面"。
    """
    try:
        from PIL import Image
    except Exception:            # noqa: BLE001 —— 没装 Pillow 不该让合成崩
        return None
    try:
        im = Image.open(png_path)
    except Exception:            # noqa: BLE001
        return None
    try:
        alpha = im.convert("RGBA").getchannel("A")
    except Exception:            # noqa: BLE001
        return None
    w, h = alpha.size
    if not w or not h:
        return None
    # 统计"明显不透明"的像素（alpha >= 32）：抗锯齿边缘的 1~2 级不算覆盖
    hist = alpha.histogram()
    opaque = sum(hist[32:])
    cw, ch = canvas if canvas else (1920, 1080)
    return round(opaque / float(max(1, cw) * max(1, ch)), 4)


def audit_overlay(windows, tl, root, canvas=(1920, 1080)):
    """按预算审计叠加层，返回 {errors, warnings, stats}。

    windows 是 resolve_windows() 的结果（已含 start/end），所以这里只做度量与判定。
    """
    rows = tl.get("lines") or []
    total = float(tl.get("total") or 0.0)
    n_lines = max(1, len(rows))
    errors, warnings = [], []
    stats = {"layers": len(windows), "lines": len(rows), "total_sec": round(total, 3),
             "per_line": {}, "heavy_lines": {}, "area": {}, "covered_lines": 0,
             "covered_ratio": 0.0, "time_weight": 0.0, "no_pil": False}
    if not windows:
        return {"errors": errors, "warnings": warnings, "stats": stats}
    time_weight = 0.0
    covers = [w for w in windows if str(w.get("role") or "chip") == "cover"]
    stats["cover_layers"] = [w["id"] for w in covers]
    if len(covers) > OVL_COVER_MAX:
        errors.append("封面/章节卡声明了 %d 层（上限 %d）：role=cover 是给开场/转场的，不能每句都用"
                      % (len(covers), OVL_COVER_MAX))
    for w in windows:
        png = _layer_png(w, root)
        cov = layer_coverage(png, canvas)
        if cov is None:
            stats["no_pil"] = True
            continue
        stats["area"][w["id"]] = cov
        # 每句层数**先**统计：它和面积无关（三个小卡叠在一起同样是"回到卡片"），
        # 必须在"小面积 -> 跳过"之前做，否则这条判据永远不触发。
        _owner, _best = None, 0.0
        for _r in rows:
            _s0 = float(_r.get("start") or 0.0)
            _s1 = float(_r.get("end") or 0.0)
            _ov = max(0.0, min(w["end"], _s1) - max(w["start"], _s0))
            if _ov > _best:
                _owner, _best = str(_r.get("id")), _ov
        if _owner:
            stats["per_line"][_owner] = stats["per_line"].get(_owner, 0) + 1
            # 覆盖率只算"重层"（面积 >= 6%）：一行小对照/一个角标的信息量等于一句字幕，
            # 不该被当成"盖住素材"。重层才是判定"素材还是画面"的对象。
            if cov >= OVL_COUNT_AREA:
                stats["heavy_lines"][_owner] = stats["heavy_lines"].get(_owner, 0) + 1
                time_weight += max(0.0, float(w["end"]) - float(w["start"]))
        if str(w.get("role") or "chip") == "cover":
            if cov > OVL_COVER_AREA_MAX:
                errors.append("封面层 %s 覆盖 %.1f%%（封面上限 %.0f%%）"
                              % (w["id"], cov * 100, OVL_COVER_AREA_MAX * 100))
            continue
        # 面积小于 OVL_COUNT_AREA 的当"标注"（等同字幕），不参与面积告警，但仍计入每句层数
        if cov < OVL_COUNT_AREA:
            stats.setdefault("chips", []).append(w["id"])
            continue
        if cov >= OVL_AREA_FAIL:
            errors.append("层 %s 覆盖 %.1f%% 画面（>= %.0f%%）：素材已经被盖住，这是整屏卡片不是叠加"
                          % (w["id"], cov * 100, OVL_AREA_FAIL * 100))
        elif cov >= OVL_AREA_WARN:
            warnings.append("层 %s 覆盖 %.1f%% 画面（提示线 %.0f%%）：确认主体没被压住"
                            % (w["id"], cov * 100, OVL_AREA_WARN * 100))
    stats["covered_lines"] = len(stats["heavy_lines"])
    stats["covered_ratio"] = round(len(stats["heavy_lines"]) / float(n_lines), 3)
    stats["time_weight"] = round(time_weight / total, 3) if total > 0 else 0.0
    for lid, n in stats["per_line"].items():
        if n >= OVL_LAYERS_PER_LINE_FAIL:
            errors.append("句 %s 同时叠了 %d 层（>= %d）：每句只该有 1 个信息元素"
                          % (lid, n, OVL_LAYERS_PER_LINE_FAIL))
    if stats["covered_ratio"] >= OVL_LINES_FAIL:
        errors.append("%.0f%% 的句子都有叠加层（>= %.0f%%）：全片都在叠，素材成了背景板"
                      % (stats["covered_ratio"] * 100, OVL_LINES_FAIL * 100))
    elif stats["covered_ratio"] >= OVL_LINES_WARN:
        warnings.append("%.0f%% 的句子都有叠加层（提示线 %.0f%%）：留几段纯素材让观众喘气"
                        % (stats["covered_ratio"] * 100, OVL_LINES_WARN * 100))
    if stats["time_weight"] >= OVL_TIME_WARN:
        warnings.append("叠加覆盖了 %.0f%% 的片长（提示线 %.0f%%）"
                        % (stats["time_weight"] * 100, OVL_TIME_WARN * 100))
    return {"errors": errors, "warnings": warnings, "stats": stats}

# ============================================================================
# 二、各阶段
# ============================================================================

def st_silent(work, footage, force=False):
    """按 cut.json 拼接无声底片（流拷贝），并核对帧数 == round(total*fps)。"""
    work = Path(work)
    cut = _load_json(work / "cut.json")
    tl = _load_json(work / "timeline.json")
    if not cut or not tl:
        raise SystemExit("[compose] 缺 %s/cut.json 或 timeline.json（先跑 footage.py --stage cut）" % work)
    fps = float((footage.get("video") or {}).get("fps") or 30)
    total = float(tl.get("total") or 0.0)
    want_frames = int(round(total * fps))
    have = sum(int(r.get("frames") or 0) for r in cut)
    if not have:
        have = int(round(sum(float(r.get("len") or 0) for r in cut) * fps))
    if want_frames and abs(have - want_frames) > 1:
        raise SystemExit(
            "[compose] 镜头总帧数 %d 与时间轴 %d 帧不符（差 %d 帧 / %.3fs）。\n"
            "  音画会随片长累积错位。修法：重跑 footage.py --stage cut（新实现按累计帧切）；\n"
            "  若 cut.json 是旧版（没有 frames 字段），必须先重切。"
            % (have, want_frames, have - want_frames, (have - want_frames) / fps))
    dst = work / "silent.mp4"
    if dst.exists() and not force:
        d = media_dur(dst)
        if d > 0:
            log("  [silent] 复用 %s（%.3fs）" % (dst.name, d))
            return str(dst)
    lst = work / "concat_compose.txt"
    lst.write_text("\n".join("file '%s'" % Path(r["video"]).resolve().as_posix()
                             for r in cut) + "\n", encoding="utf-8")
    _run([K.FFMPEG, "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0",
          "-i", str(lst), "-c", "copy", str(dst)])
    d = media_dur(dst)
    if d <= 0:
        raise SystemExit("[compose] 拼接后读不到时长，拼接失败")
    log("  [silent] %d 镜 -> %.3fs（%d 帧，时间轴 %d 帧）" % (len(cut), d, have, want_frames))
    return str(dst)


def st_audio(work, mix_name, target_i, target_tp, force=False):
    """mix.wav -> audio_norm.wav（两遍 loudnorm）。成败都要明确。"""
    work = Path(work)
    mix = work / mix_name
    if not mix.exists():
        raise SystemExit("[compose] 缺 %s（先跑 footage.py --stage mix）" % mix)
    dst = work / "audio_norm.wav"
    if dst.exists() and not force:
        log("  [audio] 复用 %s" % dst.name)
        return str(dst)
    af = K.loudnorm_two_pass(str(mix), target_i=target_i, target_tp=target_tp)
    if not af:
        raise SystemExit("[compose] loudnorm 测量失败（全静音或音频坏）：%s" % mix)
    _run([K.FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(mix), "-af", af,
          "-ar", "48000", "-ac", "2", str(dst)])
    log("  [audio] 两遍 loudnorm -> %s（I=%.1f / TP=%.1f）" % (dst.name, target_i, target_tp))
    return str(dst)


def _layer_png(ly, root):
    png = Path(ly["png"])
    return png if png.is_absolute() else Path(root) / ly["png"]


def st_compose(footage, work, out, spec, args):
    """一次编码：底片 -> 按时间窗叠层 -> 烧字幕 -> 合音轨。"""
    work = Path(work); out = Path(out)
    silent = (work / "silent.mp4").resolve()
    audio = (work / "audio_norm.wav").resolve()
    for p in (silent, audio):
        if not p.exists():
            raise SystemExit("[compose] 缺 %s（先跑 --stage silent/audio）" % p)
    tl = _load_json(work / "timeline.json") or {}
    # 缺图层的 png 不能静默丢弃：那样"写错路径的那一层"既不报错、也不进审计、成片里少了东西
    # 没人知道。契约错误统一 exit 2（与 docstring 承诺一致），用户改路径就行。
    resolved = resolve_windows(spec, tl)
    missing = [(ly.get("id"), _layer_png(ly, args.root)) for ly in resolved
               if not _layer_png(ly, args.root).exists()]
    if missing:
        for _mid, _p in missing:
            log("  [契约错误] 图层 %s 的 png 不存在：%s" % (_mid, _p))
        raise SystemExit(2)
    layers = resolved
    # 叠之前先算预算：叠多了就是"整屏卡片"，合成阶段是最后一道能拦住它的地方
    _vw = int((footage.get("video") or {}).get("width") or 1920)
    _vh = int((footage.get("video") or {}).get("height") or 1080)
    audit = audit_overlay(layers, tl, args.root, canvas=(_vw, _vh))
    for wmsg in audit["warnings"]:
        log("  [overlay WARN] " + wmsg)
    if audit["stats"].get("no_pil"):
        log("  [overlay WARN] 没装 Pillow，量不出层面积（这条预算检查本次未执行）")
    if audit["errors"]:
        for emsg in audit["errors"]:
            log("  [overlay FAIL] " + emsg)
        if not getattr(args, "allow_overlay_budget", False):
            raise SystemExit("[compose] 叠加预算超限（见上）；确需如此请加 --allow-overlay-budget")
    _write_json(work / "overlay_audit.json", audit)
    vd, ad = media_dur(silent), media_dur(audio)
    if vd <= 0 or ad <= 0:
        raise SystemExit("[compose] 读不到底片/音轨时长（%.3f / %.3f）" % (vd, ad))
    fps = float((footage.get("video") or {}).get("fps") or 30)
    pad = max(0.0, ad - vd)
    chain = ["[0:v]fps=%g,format=yuv420p%s[base]"
             % (fps, (",tpad=stop_mode=clone:stop_duration=%.3f" % pad) if pad > 0.01 else "")]
    cur = "base"
    for i, ly in enumerate(layers, start=1):
        span = ly["end"] - ly["start"]
        fade = min(ly["fade"], span / 2.0)
        sub = ["format=rgba", "trim=0:%.3f" % span,
               "setpts=PTS-STARTPTS+%.3f/TB" % ly["start"]]
        if fade > 0:
            sub.append("fade=t=in:st=%.3f:d=%.3f:alpha=1" % (ly["start"], fade))
            sub.append("fade=t=out:st=%.3f:d=%.3f:alpha=1" % (ly["end"] - fade, fade))
        chain.append("[%d:v]" % i + ",".join(sub) + "[ly%d]" % i)
        chain.append("[%s][ly%d]overlay=x=%s:y=%s:eval=init:"
                     "enable='between(t,%.3f,%.3f)'[v%d]"
                     % (cur, i, ly["x"], ly["y"], ly["start"], ly["end"], i))
        cur = "v%d" % i
    sub_ass = spec.get("subtitle")
    ap = (work / sub_ass) if sub_ass else None
    if ap is not None and ap.exists():
        chain.append("[%s]ass=filename='%s',format=yuv420p[vout]" % (cur, ap.name))
        vf_cwd = str(ap.parent)
    else:
        chain.append("[%s]format=yuv420p[vout]" % cur)
        vf_cwd = str(work)
    cmd = [K.FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", str(silent)]
    for ly in layers:
        cmd += ["-loop", "1", "-framerate", "%g" % fps, "-i", str(_layer_png(ly, args.root))]
    cmd += ["-i", str(audio)]
    audio_idx = len(layers) + 1
    out = Path(out).resolve()
    cmd += ["-filter_complex", ";".join(chain),
            "-map", "[vout]", "-map", "%d:a" % audio_idx,
            "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", "-t", "%.3f" % ad,
            "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(cmd, cwd=vf_cwd)
    d = media_dur(out)
    if d <= 0:
        raise SystemExit("[compose] 输出读不到时长：%s" % out)
    log("  [compose] %d 层 -> %s（%.3fs / %.1f MB）"
        % (len(layers), out.name, d, out.stat().st_size / 1048576))
    return str(out)


def st_qa(out, work, args):
    """调 qa_frames 做帧级深检；素材线硬切用 --shots 传切点，避免误报闪烁。"""
    work = Path(work)
    tl = _load_json(work / "timeline.json") or {}
    rows = tl.get("lines") or []
    shots = [round(float(r["start"]), 3) for r in rows[1:]]
    shots_json = work / "shot_cuts.json"
    _write_json(shots_json, shots)
    cmd = [sys.executable, os.path.join(HERE, "qa_frames.py"), str(Path(out).resolve()),
           "--shots", str(shots_json), "--out", str(work / "qa" / "deep.json")]
    # 必须显式 utf-8：子进程输出是 UTF-8，Windows 默认按 GBK 解码会出乱码
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (p.stdout or "") + (p.stderr or "")
    log("\n".join(tail.strip().splitlines()[-12:]))
    return p.returncode


# ============================================================================
# 三、CLI
# ============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="compose.py",
        description="素材打底 + 小面积叠加（hybrid 第三种形态）：按时间窗叠层 + 烧字幕 + 强制响度归一")
    ap.add_argument("--footage", default="footage.json", help="素材线契约（取 video.fps 等）")
    ap.add_argument("--overlay", default="overlay.json", help="叠加层契约（可缺省 = 不叠任何层）")
    ap.add_argument("--work", default="work", help="中间产物目录")
    ap.add_argument("--out", default="out/film.mp4", help="成片路径")
    ap.add_argument("--root", default=".", help="工程根（overlay.layers[].png 的相对基准）")
    ap.add_argument("--stage", default="all", choices=("all",) + STAGES)
    ap.add_argument("--mix", default="mix.wav", help="混音结果（默认 mix.wav）")
    ap.add_argument("--target-i", type=float, default=-16.0, help="目标积分响度 LUFS")
    ap.add_argument("--target-tp", type=float, default=DEFAULT_TARGET_TP,
                    help="目标真峰 dBTP（默认 %.1f：AAC 会抬高 0.5~1.1 dB）" % DEFAULT_TARGET_TP)
    ap.add_argument("--crf", type=int, default=20, help="视频 CRF（默认 20）")
    ap.add_argument("--preset", default="medium", help="x264 preset")
    ap.add_argument("--force", action="store_true", help="忽略已有中间产物，强制重跑本阶段")
    ap.add_argument("--allow-overlay-budget", action="store_true",
                    help="叠加预算超限也继续（work/overlay_audit.json 仍记 error）")
    args = ap.parse_args(argv)
    _utf8_stdout()

    foot = _load_json(args.footage) or {}
    work = Path(args.work).resolve()
    args.root = str(Path(args.root).resolve())
    args.out = str(Path(args.out).resolve())
    spec = {"layers": []}
    if args.overlay and Path(args.overlay).exists():
        spec = load_overlay(args.overlay)
    elif args.stage in ("all", "compose"):
        log("  [WARN] 没找到 %s：按「不叠任何层」处理" % args.overlay)

    def do(s):
        return args.stage in ("all", s)

    try:
        if do("overlay"):
            tl = _load_json(work / "timeline.json") or {}
            wins = resolve_windows(spec, tl)
            _write_json(work / "overlay_windows.json", wins)
            log("== overlay：%d 层绑定时间窗" % len(wins))
            for w in wins:
                log("   %-10s %.2f ~ %.2f s  y=%s" % (w["id"], w["start"], w["end"], w["y"]))
        if do("silent"):
            log("== silent：拼接底片")
            st_silent(work, foot, force=args.force)
        if do("audio"):
            log("== audio：响度归一")
            st_audio(work, args.mix, args.target_i, args.target_tp, force=args.force)
        if do("compose"):
            log("== compose：叠加 + 字幕 + 封装")
            st_compose(foot, work, args.out, spec, args)
        if args.stage in ("all", "qa"):
            log("== qa：帧级深检")
            rc = st_qa(args.out, work, args)
            if rc != 0:
                log("[compose] 质检未过（见上）")
                return rc
    except SystemExit as e:
        # 保留契约错误的退出码 2（见 docstring：0 成功 / 1 失败 / 2 契约错误）。
        # 以前一律 return 1，调用方按退出码区分"参数写错"与"跑挂了"永远失败。
        code = e.code if isinstance(e.code, int) else 1
        if not isinstance(e.code, int) and e.code is not None:
            log(str(e.code))
        return code
    log("完成：%s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
