# -*- coding: utf-8 -*-
"""footageqa.py — 素材剪辑生产线的专属门禁（F1..F7）。

现有门禁（G0 / qa_frames / qaasr / VMAF）默认"画面是代码画出来的、同一参数重渲必然一致"。
素材线的头号问题不是帧闪烁，而是**画面里不该出现的东西**：

  F1 镜头越界        EDL 里 at+len 超过源时长 -> 切出尾帧冻结/黑帧
  F2 原字幕残留      源片烧录的中文字幕没治理干净 -> 成片"两行字幕打架"
  F3 水印残留        平台台标/水印没裁掉 -> 一看就是搬运
  F4 音画等长        音频总线与视频不等长 -> 会悄悄吃掉片尾
  F5 素材溯源        没出处标注 -> 无法交付（版权与诚实两重要求）
  F6 旁白/原声比例   原片自带解说盖住旁白 -> "很吵、听不清"
  F7 人工签核        联系表 + 逐镜台词，人看过才签

三档语义与技能其他门禁一致：pass = 真的查了且通过；fail = 查了没过；
skipped = 这次没执行（缺素材 / 显式跳过 / 内容已变），**skipped 不等于通过**。

设计取舍：
  * 不依赖 cv2：F2/F3 用 numpy 的行边缘能量与跨帧方差，缺 cv2 也能跑。
  * 每条门禁输出实测值，不是只给结论——"看起来没问题"不是凭据。
  * 门禁与签核绑内容指纹：改了 footage.json 或 EDL，旧签核自动失效。
  * 采样区一律先定位**活动画面区**（_longest_active_run）：素材线的成片有遮幅，
    直接按成片画布比例取样会把黑边当成画面（F2/F3 第一版都栽在这）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filmkit      # noqa: E402
import footageedit  # noqa: E402
import footagekit   # noqa: E402

# F2 阈值：画面内容区"底部 / 中部"的行边缘能量比值上限。
# 实测：裁干净 0.3–0.9；源字幕没裁干净 1.7–3.8。
SUB_RATIO_MAX = 1.25
# F3 阈值：右上角区域边缘能量（有结构）+ 静止度（比画面中部更不动多少倍）。
WM_ENERGY_MIN = 0.16
WM_STILLNESS_MIN = 2.5


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 抽帧与度量
def _grab_gray(path, t, w=320):
    """抽一帧转灰度 numpy（缩到 w 宽）。ffmpeg 直出 rawvideo，缺 cv2 也能跑。"""
    import numpy as np
    p = subprocess.run([filmkit.FFMPEG, "-v", "error", "-ss", "%.3f" % max(0.0, float(t)),
                        "-i", str(path), "-frames:v", "1", "-vf", "scale=%d:-2" % w,
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return None
    a = np.frombuffer(p.stdout, dtype=np.uint8)
    if a.size % w:
        return None
    return a.reshape(-1, w).astype(np.float32)


def _row_edge_energy(img):
    """按行算横向边缘能量（相邻像素绝对差的行均值）。规则文字带的行有明显峰。"""
    import numpy as np
    return np.abs(np.diff(img, axis=1)).mean(axis=1)


def _longest_active_run(luma, spread, min_rows=40):
    """最长的一段连续"有内容"的行区间 (y0, y1)。

    成片结构是：上遮幅(黑) | 画面 | 下遮幅(黑) | 自产字幕。
    取最长连续段才能稳定落在画面本身；用 min..max 会把自产字幕也圈进来
    （F2 第一版就栽在这：把自家字幕当成源字幕，比值 3.82 必然 fail）。
    """
    # 阈值刻意放低：雾山、夜景、纯色暗部都是真实素材的常态，
    # 用 4.0/10 会把"低对比度但正常"的画面判成黑边，采样区漂到字幕带上。
    active = (luma > 8.0) & (spread > 2.0)
    best = (0, 0, 0)
    run = None
    for y in range(active.size):
        if active[y]:
            if run is None:
                run = y
        elif run is not None:
            if y - run > best[0]:
                best = (y - run, run, y)
            run = None
    if run is not None and active.size - run > best[0]:
        best = (active.size - run, run, active.size)
    if best[0] < min_rows:
        return None
    return best[1], best[2]


def _fingerprint(foot, edl):
    """签核指纹：契约 + EDL 的内容哈希。改内容 -> 旧签核自动失效。"""
    h = hashlib.sha1()
    payload = {"title": foot.get("title"),
               "treatment": foot.get("treatment"),
               "subtitle": foot.get("subtitle"),
               "lines": [{k: v for k, v in ln.items() if k in ("id", "text", "sub", "shot")}
                         for ln in foot.get("lines") or []],
               "shots": [[s.get("id"), s.get("source"), s.get("at"), s.get("len")]
                         for s in (edl or {}).get("shots") or []]}
    h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- F1
def f1_edl(foot, work, out, tl, edl, ctx):
    srcs_info = {}
    sp = Path(work) / "sources.json"
    if sp.exists():
        srcs_info = json.loads(sp.read_text(encoding="utf-8"))
    shots = (edl or {}).get("shots") or []
    problems = footageedit.check_edl(edl, foot, sources_info=srcs_info) if shots else ["没有 EDL"]
    if problems:
        return dict(gate="F1", status="fail", detail="；".join(problems[:3]),
                    values={"problems": problems})
    total = sum(s["len"] for s in shots)
    return dict(gate="F1", status="pass",
                detail="%d 镜 / %.2fs，无越界" % (len(shots), total),
                values={"shots": len(shots), "sum_len": round(total, 2),
                        "timeline_total": tl.get("total")})


# ---------------------------------------------------------------- F2/F3 共用
def _parse_crop(treatment):
    """解析 treatment.crop -> (x, y, w, h)；没写或格式不对返回 None。"""
    c = str((treatment or {}).get("crop") or "").strip()
    parts = c.split(":")
    if len(parts) != 4:
        return None
    try:
        w, h, x, y = (int(float(p)) for p in parts)
    except ValueError:
        return None
    return x, y, w, h


def _visible_box(foot):
    """素材视角下"最终会出现在画面里"的矩形 (x0, y0, x1, y1)，坐标是**源像素**。

    素材线的画面治理是"crop -> scale -> pad"，所以成片可见区 = crop 框本身；
    scale/pad 只是把它装进输出画布，不会把裁掉的内容加回来。
    没写 crop 时返回 None：源分辨率各不相同，算不出"整帧"到底是多大；调用方按
    skipped 处理（可见区未知 != 通过）。
    """
    treatment = foot.get("treatment") or {}
    crop = _parse_crop(treatment)
    if crop:
        x, y, w, h = crop
        return x, y, x + w, y + h
    return None


def _declared_band(src, key):
    """取 sources[].burned_subs / watermark 的声明值。

    两种写法：
      "burned_subs": {"y0": 740, "y1": 824}       # 字幕带的纵向范围
      "watermark":   {"x": 1580, "y": 16, "w": 316, "h": 44}
    也可以用 probe 的建议值（--stage probe 会打印）直接粘进来。
    """
    v = src.get(key)
    if not v:
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return {"y0": v[0], "y1": v[1]} if key == "burned_subs" else {"x": v[0], "y": v[1]}
    return None


def _overlaps(box, band, key):
    """声明区域是否与可见区相交（相交=没治理干净）。"""
    if not box or not band:
        return None
    x0, y0, x1, y1 = box
    if key == "burned_subs":
        b0 = float(band.get("y0", band.get("y", 0)))
        b1 = float(band.get("y1", b0 + band.get("h", 0) or 0))
        if b1 <= b0:
            return None
        return not (b1 <= y0 or b0 >= y1), (b0, b1)
    wx = float(band.get("x", 0)); wy = float(band.get("y", 0))
    ww = float(band.get("w", 0)); wh = float(band.get("h", 0))
    if ww <= 0 or wh <= 0:
        return None
    return (not (wx + ww <= x0 or wx >= x1 or wy + wh <= y0 or wy >= y1)), (wx, wy, ww, wh)


# ---------------------------------------------------------------- F2 原字幕残留
def f2_subtitle_bleed(foot, work, out, tl, edl, ctx):
    """烧录字幕有没有被治理掉。

    判据是**几何的**，不是"看画面像不像有字"：
      1. 每个源用它自己的分辨率声明字幕带（sources[].burned_subs，单位=源像素，
         通常由 --stage probe 的 suggest_treatment 给出建议值）；
      2. 算 treatment.crop 之后**还剩哪些源像素可见**；
      3. 声明带与可见区相交 -> 没治理干净，fail（报告里给出带子与可见区的具体区间）。

    为什么不用图像判据：实测下来"底部/中部能量比"在真实素材上正反例区间重叠
    （已治理成片 1.41、未治理原片 1.81），任何阈值都会误杀其中一类。
    门禁必须能明确回答"该裁的地方裁了没有"，而这个答案只有几何知道。
    同时仍会输出可疑度指标（底部亮像素占比）作为人工复核线索，但不参与判定。
    """
    box = _visible_box(foot)
    srcs = footagekit.source_map(foot)
    problems, checked = [], 0
    for sid, s in srcs.items():
        band = _declared_band(s, "burned_subs")
        if not band:
            continue
        checked += 1
        rec = _overlaps(box, band, "burned_subs")
        if rec is None:
            continue
        hit, rng = rec
        if hit:
            problems.append("%s：字幕带 y=%.0f..%.0f 仍在可见区 y=%.0f..%.0f 内（crop 没盖住）"
                            % (sid, rng[0], rng[1], box[1], box[3]))
    suspicion = None
    times = ctx.get("sample_times") or []
    if times:
        vals = []
        for t in times:
            img = _grab_gray(out, t, w=480)
            if img is None:
                continue
            run = _longest_active_run(img.mean(axis=1), img.std(axis=1))
            if not run:
                continue
            y0, y1 = run
            band = img[y1 - max(6, int((y1 - y0) * 0.18)): y1]
            if band.size:
                vals.append(float((band > 190).mean()))
        if vals:
            suspicion = round(max(vals), 4)
    if problems:
        return dict(gate="F2", status="fail", detail="；".join(problems[:3]),
                    values={"problems": problems, "visible_box": box,
                            "bright_suspicion": suspicion})
    if box is None:
        return dict(gate="F2", status="skipped",
                    detail="没有 treatment.crop（可见区未知）；无法判定字幕带是否被裁掉。"
                           "补上 crop 后重跑",
                    values={"bright_suspicion": suspicion})
    if not checked:
        return dict(gate="F2", status="skipped",
                    detail="没有源声明 burned_subs（字幕带位置）；无法判定是否治理干净。"
                           "用 --stage probe 的建议值填进 sources[].burned_subs 后重跑",
                    values={"bright_suspicion": suspicion})
    return dict(gate="F2", status="pass",
                detail="%d 个源的烧录字幕带都在 crop 之外（可见区 y=%.0f..%.0f）"
                       % (checked, box[1], box[3]),
                values={"visible_box": box, "bright_suspicion": suspicion})


# ---------------------------------------------------------------- F3 水印残留
def f3_watermark(foot, work, out, tl, edl, ctx):
    """平台水印/台标有没有被治理掉（同样是几何判定）。

    水印是"位置固定、每帧都印着"的东西，但它常常压在画面内的角落，
    只靠"边缘能量 + 跨帧方差"这种图像指标，会把"静止的云/静止的天空"一起判进来
    （实测：真实素材上正反例的静度指标 0.47 vs 1.07，重叠且方向不稳定）。
    所以这里同样要求源声明水印框 sources[].watermark，然后判"它还在不在可见区里"。
    """
    box = _visible_box(foot)
    srcs = footagekit.source_map(foot)
    problems, checked = [], 0
    for sid, s in srcs.items():
        band = _declared_band(s, "watermark")
        if not band:
            continue
        checked += 1
        rec = _overlaps(box, band, "watermark")
        if rec is None:
            continue
        hit, rng = rec
        if hit:
            problems.append("%s：水印 x=%.0f..%.0f y=%.0f..%.0f 仍在可见区 x=%.0f..%.0f y=%.0f..%.0f 内"
                            % (sid, rng[0], rng[0] + rng[2], rng[1], rng[1] + rng[3],
                               box[0], box[2], box[1], box[3]))
    if problems:
        return dict(gate="F3", status="fail", detail="；".join(problems[:3]),
                    values={"problems": problems, "visible_box": box})
    if box is None:
        return dict(gate="F3", status="skipped",
                    detail="没有 treatment.crop（可见区未知）；无法判定水印是否被裁掉。"
                           "补上 crop 后重跑",
                    values={"visible_box": None})
    if not checked:
        return dict(gate="F3", status="skipped",
                    detail="没有源声明 watermark（水印框）；无法判定水印是否去掉。"
                           "用 --stage probe 的建议值填进 sources[].watermark 后重跑",
                    values={"visible_box": box})
    return dict(gate="F3", status="pass",
                detail="%d 个源的水印框都在 crop 之外（可见区 x=%.0f..%.0f y=%.0f..%.0f）"
                       % (checked, box[0], box[2], box[1], box[3]),
                values={"visible_box": box})


# ---------------------------------------------------------------- F4
def f4_av_equal(foot, work, out, tl, edl, ctx):
    raw = filmkit.probe(str(out)) or {}
    streams = raw.get("streams") or []
    vdur = adur = None
    for s in streams:
        if s.get("codec_type") == "video" and s.get("duration"):
            vdur = float(s["duration"])
        elif s.get("codec_type") == "audio" and s.get("duration"):
            adur = float(s["duration"])
    fmt_dur = float((raw.get("format") or {}).get("duration") or 0.0)
    if vdur is None:
        vdur = fmt_dur or None
    if adur is None:
        adur = fmt_dur or None
    if not vdur or not adur:
        return dict(gate="F4", status="skipped", detail="读不到流时长", values={})
    diff = abs(vdur - adur)
    ok = diff <= 0.05
    return dict(gate="F4", status="pass" if ok else "fail",
                detail="video %.3fs / audio %.3fs（差 %.3fs）" % (vdur, adur, diff),
                values={"video": round(vdur, 3), "audio": round(adur, 3),
                        "diff": round(diff, 3), "threshold": 0.05})


# ---------------------------------------------------------------- F5
def f5_provenance(foot, work, out, tl, edl, ctx):
    missing = []
    for s in foot.get("sources") or []:
        if not str(s.get("license_note") or "").strip():
            missing.append(str(s.get("id")))
    credit = (foot.get("credit") or {}).get("text")
    if not str(credit or "").strip():
        missing.append("credit.text（片尾出处卡）")
    if missing:
        return dict(gate="F5", status="fail",
                    detail="缺出处：%s" % "、".join(missing[:4]),
                    values={"missing": missing})
    return dict(gate="F5", status="pass",
                detail="%d 个源都有 license_note，且有片尾出处卡"
                       % len(foot.get("sources") or []),
                values={"credit": credit})


# ---------------------------------------------------------------- F6
def _rms(x):
    import numpy as np
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def f6_voice_dominance(foot, work, out, tl, edl, ctx):
    """台词活跃区 vs 旁白间隙区的能量差（dB）。

    素材线最常见的失败是"原片自带解说 + 我们的旁白 = 两条人声互相盖"。
    在成片上量：旁白窗口内 RMS / 间隙内 RMS，差 12 dB 以上才算旁白稳压背景
    （与零素材线 voice_duck 同一口径）。
    """
    import numpy as np
    # 必须看返回码：只取 stdout 时 ffmpeg 解码失败会得到空数组，于是这条判据被记成
    # skipped（"没执行"）——而 skipped 在 F6 这类门禁里等于静默放行。解码失败是**判据
    # 无法执行的原因**，要么 fail（明确错），要么在 detail 里说清；这里按 fail 处理，
    # 因为成片音轨解不出来本身就该拦（与 preflight.py 探测 ffmpeg 的口径一致）。
    proc = subprocess.run([filmkit.FFMPEG, "-v", "error", "-i", str(out), "-vn",
                           "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                          capture_output=True)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return dict(gate="F6", status="fail",
                    detail="音轨解码失败（ffmpeg rc=%d）：%s"
                           % (proc.returncode, (tail[-1] if tail else "无 stderr")[:160]),
                    values={"ffmpeg_rc": proc.returncode})
    a = np.frombuffer(proc.stdout, dtype=np.float32)
    if a.size < 16000:
        return dict(gate="F6", status="skipped",
                    detail="音轨短于 1 秒（%.2fs），量不出旁白/背景比" % (a.size / 16000.0),
                    values={"samples": int(a.size)})
    sr = 16000
    chans, gaps = [], []
    prev = 0.0
    for r in tl.get("lines") or []:
        s0, s1 = int(float(r["start"]) * sr), int(float(r["end"]) * sr)
        if s1 > s0:
            chans.append(a[s0:min(s1, a.size)])
        g0, g1 = int(prev * sr), int(float(r["shot_start"]) * sr)
        if g1 - g0 > sr // 2:
            gaps.append(a[g0:g1])
        prev = float(r["shot_end"])
    if not chans or not gaps:
        return dict(gate="F6", status="skipped",
                    detail="没有足够的旁白/间隙窗口（旁白首尾相接）", values={})
    v = _rms(np.concatenate(chans))
    g = _rms(np.concatenate(gaps))
    if g <= 0:
        return dict(gate="F6", status="pass", detail="间隙区静音（背景没有盖旁白）",
                    values={"voice_rms": round(v, 5), "gap_rms": 0.0})
    db = float(20.0 * np.log10(max(v, 1e-9) / max(g, 1e-9)))
    ok = db >= 12.0
    return dict(gate="F6", status="pass" if ok else "fail",
                detail="旁白比背景高 %.1f dB（阈值 12 dB）" % db,
                values={"ratio_db": round(db, 2), "threshold": 12.0,
                        "voice_rms": round(v, 5), "gap_rms": round(g, 5)})


# ---------------------------------------------------------------- F8
def f8_source_variety(foot, work, out, tl, edl, ctx):
    """素材量够不够：按片长反算独立来源数，防止"一张图平移到底"。

    三档：
      * distinct < budget.minimum        -> fail（明显偷懒：2 张图撑 6 分钟）
      * distinct < budget.recommended    -> pass + warning（画面重复度偏高）
      * 单源连续 >= 3 镜 / 重复取景窗 > 0 -> 附在 warning 里，指出是哪一段
    逃生口：footage.json 写 licensing.allow_sparse_sources=true 才记 skipped，
    并在 detail 里写明理由——**不许默认静默放过**（这正是本次要堵的口子）。
    """
    total = float((tl or {}).get("total") or 0.0)
    if total <= 0:
        return dict(gate="F8", status="skipped", detail="没有时间轴，量不出素材密度",
                    values={})
    usage = footagekit.source_usage(edl)
    budget = footagekit.source_budget(total)
    values = {"total_sec": budget["total_sec"], "distinct": usage["distinct"],
              "shots": usage["shots"], "budget": budget,
              "max_consecutive": usage["max_consecutive"],
              "duplicate_windows": usage["duplicate_windows"],
              "per_source": usage["per_source"]}
    lic = (foot or {}).get("licensing") or {}
    if lic.get("allow_sparse_sources"):
        return dict(gate="F8", status="skipped",
                    detail="已声明允许多次复用素材（why: %s）；%d 个来源 / %.1fs（推荐 %d、下限 %d）"
                           % (str(lic.get("why") or "未写 reason")[:80], usage["distinct"],
                              budget["total_sec"], budget["recommended"], budget["minimum"]),
                    values=values)
    if usage["distinct"] < budget["minimum"]:
        return dict(gate="F8", status="fail",
                    detail="素材太少：%d 个独立来源 / %.1fs（硬下限 %d、推荐 %d）。"
                           "画面会全程是同一张图在平移——补素材，或写 licensing.allow_sparse_sources 显式豁免"
                           % (usage["distinct"], budget["total_sec"],
                              budget["minimum"], budget["recommended"]),
                    values=values)
    notes = []
    if usage["distinct"] < budget["recommended"]:
        notes.append("低于推荐密度 %d（建议再补 %d 个来源）"
                     % (budget["recommended"], budget["recommended"] - usage["distinct"]))
    if usage["max_consecutive"] >= 3:
        notes.append("有素材连续用了 %d 镜（同一段画面停留过久）" % usage["max_consecutive"])
    if usage["duplicate_windows"] > 0:
        notes.append("%d 处取景窗重复（同源 at 相差 <0.5s）" % usage["duplicate_windows"])
    if notes:
        return dict(gate="F8", status="pass",
                    detail="%d 个来源 / %.1fs（推荐 %d、下限 %d）；%s"
                           % (usage["distinct"], budget["total_sec"],
                              budget["recommended"], budget["minimum"], "；".join(notes)),
                    values=values)
    return dict(gate="F8", status="pass",
                detail="%d 个来源 / %.1fs（推荐 %d、下限 %d）"
                       % (usage["distinct"], budget["total_sec"],
                          budget["recommended"], budget["minimum"]),
                values=values)

# ---------------------------------------------------------------- F7
def f7_signoff(foot, work, out, tl, edl, ctx):
    # 抽帧失败 = 联系表少了镜 = "人看过"这件事没覆盖全片。这是 fail，不是 skipped：
    # skipped 会被 --strict-qa 之外的路径当成"没执行"，而这里明明是**执行了但证据不全**。
    snap_fails = list(ctx.get("snap_fails") or [])
    if snap_fails:
        return dict(gate="F7", status="fail",
                    detail="联系表有 %d 镜抽帧失败（签核证据不全）：%s"
                           % (len(snap_fails), "；".join(snap_fails[:3])),
                    values={"snap_fails": snap_fails[:10]})
    sheet = ctx.get("contact_sheet")
    if not sheet or not Path(sheet).exists():
        if ctx.get("shots_expected"):
            return dict(gate="F7", status="fail",
                        detail="联系表没有生成（%d 镜全部抽帧失败），签核证据缺失"
                               % int(ctx["shots_expected"]), values={})
        return dict(gate="F7", status="skipped", detail="联系表还没生成", values={})
    fp = _fingerprint(foot, edl)
    so = Path(work) / "footage-signoff.json"
    if not so.exists():
        return dict(gate="F7", status="skipped",
                    detail="未签核（看过 %s 后写 work/footage-signoff.json）"
                           % Path(sheet).name,
                    values={"fingerprint": fp, "contact_sheet": str(sheet)})
    try:
        d = json.loads(so.read_text(encoding="utf-8"))
    except ValueError:
        return dict(gate="F7", status="fail", detail="签核文件不是合法 JSON", values={})
    if str(d.get("fingerprint") or "") != fp:
        return dict(gate="F7", status="skipped",
                    detail="内容已变（指纹 %s != 签核时 %s），旧签核失效，需重新看片"
                           % (fp, d.get("fingerprint")), values={"fingerprint": fp})
    if d.get("verdict") != "approved":
        return dict(gate="F7", status="fail", detail="签核结论是 %r" % d.get("verdict"),
                    values={"fingerprint": fp})
    return dict(gate="F7", status="pass",
                detail="已签核（%s，%s）" % (d.get("by") or "user", d.get("note") or ""),
                values={"fingerprint": fp})


GATES = [f1_edl, f2_subtitle_bleed, f3_watermark, f4_av_equal,
         f5_provenance, f6_voice_dominance, f7_signoff, f8_source_variety]


# ---------------------------------------------------------------- 联系表（F7 证据）
def make_contact_sheet(out, edl, tl, path, cols=4, thumb=480, fails=None):
    """逐镜抽一帧拼联系表。这是 F7 的唯一证据，也是"人看过"的凭据。

    单镜抽帧失败**不能静默跳过**：联系表少一张图，F7 的"人看过"就少了一镜，
    而报告里看不出任何痕迹。抽帧失败记进 fails 列表（由调用方写进 F7 detail），
    全部失败时返回 None（调用方按 fail 处理）。
    """
    from PIL import Image, ImageDraw
    rows_by_id = {r["id"]: r for r in (tl or {}).get("lines") or []}
    path = Path(path)
    tiles = []
    for i, s in enumerate((edl or {}).get("shots") or []):
        r = rows_by_id.get(s["id"]) or {}
        t = float(r.get("shot_start") or 0.0) + min(1.2, max(0.0, float(s["len"]) * 0.35))
        png = path.with_name("_frame_%02d.png" % i)
        png.parent.mkdir(parents=True, exist_ok=True)
        try:
            filmkit.snap(str(out), t, str(png))
        except Exception as e:                                  # noqa: BLE001
            # 抽帧失败是「证据缺失」，不是「本镜没问题」：记下来，F7 会把它报成 fail。
            if fails is not None:
                fails.append("%s@%.2fs: %s" % (s.get("id"), t, e))
            continue
        tiles.append((png, "%s  %.1fs" % (s["id"], float(s["at"]))))
    if not tiles:
        return None
    im0 = Image.open(tiles[0][0])
    tw = int(thumb)
    th = max(2, int(round(tw * im0.height / float(im0.width))))
    rows = (len(tiles) + cols - 1) // cols
    bar = 26
    sheet = Image.new("RGB", (cols * tw, rows * (th + bar)), (10, 11, 13))
    dr = ImageDraw.Draw(sheet)
    for i, (png, label) in enumerate(tiles):
        im = Image.open(png).convert("RGB").resize((tw, th), Image.LANCZOS)
        x, y = (i % cols) * tw, (i // cols) * (th + bar)
        sheet.paste(im, (x, y))
        dr.text((x + 8, y + th + 5), label, fill=(230, 230, 230))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(path))
    for png, _ in tiles:
        try:
            png.unlink()
        except OSError:
            pass
    return str(path)


# ---------------------------------------------------------------- 报告
def write_report(out, results, extra, ctx):
    """写人读的 QC 报告（每次完整重写：报告是快照，不做增量）。"""
    tick = chr(96)
    md = ["# 素材剪辑交付报告（footage 线）", ""]
    md.append("- 运行编号：%s" % ctx.get("run_id"))
    md.append("- 成片：%s" % out)
    md.append("- 契约指纹：%s" % ctx.get("fingerprint"))
    md.append("- 生成时间：%s" % footagekit._now())
    md.append("")
    md.append("## 门禁")
    md.append("")
    md.append("| 门禁 | 结论 | 实测值 |")
    md.append("|---|---|---|")
    for r in results:
        md.append("| %s | **%s** | %s |" % (r["gate"], r["status"], r["detail"]))
    md.append("")
    n_pass = sum(1 for r in results if r["status"] == "pass")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_skip = sum(1 for r in results if r["status"] == "skipped")
    md.append("**PASS %d / FAIL %d / SKIPPED %d**（skipped 不等于通过）"
              % (n_pass, n_fail, n_skip))
    md.append("")
    if extra:
        md.append("## 通用质检")
        md.append("")
        for k in ("duration", "bitrate", "loudness", "errors", "warnings",
                  "qa_report_error"):
            if k in extra:
                md.append("- %s：%s" % (k, extra[k]))
        md.append("")
    md.append("## 人工签核")
    md.append("")
    md.append("联系表：%s" % ctx.get("contact_sheet"))
    md.append("")
    md.append("看过联系表（逐镜画面 + 台词）后，写 " + tick + "work/footage-signoff.json"
              + tick + "：")
    md.append("")
    md.append(tick * 3 + "json")
    md.append('{"fingerprint": "%s", "verdict": "approved", "by": "user", "note": "看过了"}'
              % ctx.get("fingerprint"))
    md.append(tick * 3)
    md.append("")
    p = Path(out).parent / "QC-REPORT-footage.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(md), encoding="utf-8")
    return str(p)


def run_all(foot, work, out, tl, edl, strict=False):
    """跑 F1..F7 + 通用质检，写 work/footage-gates.json 与 out/QC-REPORT-footage.md。"""
    work = Path(work)
    out = Path(out)
    run_id = "f%s" % footagekit._now().replace("-", "").replace(":", "")
    fp = _fingerprint(foot, edl)
    times = []
    rows = {r["id"]: r for r in (tl or {}).get("lines") or []}
    for s in (edl or {}).get("shots") or []:
        r = rows.get(s["id"]) or {}
        base = float(r.get("shot_start") or 0.0)
        times.append(round(base + float(s["len"]) * 0.6, 2))
    snap_fails = []
    sheet = make_contact_sheet(out, edl, tl, work / "qa" / "footage-sheet.png",
                               fails=snap_fails)
    ctx = {"run_id": run_id, "fingerprint": fp, "sample_times": times,
           "contact_sheet": sheet, "snap_fails": snap_fails,
           "shots_expected": len((edl or {}).get("shots") or [])}
    results = []
    for fn in GATES:
        gate_id = fn.__name__.split("_")[0].upper()
        try:
            r = fn(foot, work, out, tl, edl, ctx)
        except Exception as e:       # noqa: BLE001 —— 门禁自己崩了也算 fail，不许静默跳过
            r = dict(gate=gate_id, status="fail",
                     detail="门禁执行异常：%s" % e, values={})
        results.append(r)
        log("  [%s] %-8s %s" % (r["gate"], r["status"], r["detail"]))
    extra = {}
    try:
        q = filmkit.qa_report(str(out), expected_subtitles=None,
                              notes={"srt": str(work / "sub.srt")})
        extra["errors"] = q.get("errors") or []
        extra["warnings"] = q.get("warnings") or []
        extra["loudness"] = q.get("loudness") or {}
        pr = (q.get("probe") or {}).get("format") or {}
        extra["duration"] = pr.get("duration")
        extra["bitrate"] = pr.get("bit_rate")
    except Exception as e:          # noqa: BLE001
        extra["qa_report_error"] = str(e)
    payload = {"schema": 1, "run_id": run_id, "out": str(out), "fingerprint": fp,
               "gates": results, "general": extra, "contact_sheet": sheet}
    (work / "footage-gates.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    rep = write_report(out, results, extra, ctx)
    n_fail = sum(1 for r in results if r["status"] == "fail")
    log("  QC 报告 -> %s（FAIL %d）" % (rep, n_fail))
    if strict and n_fail:
        raise SystemExit("素材线门禁有 %d 项 fail（--strict-qa）" % n_fail)
    return payload
