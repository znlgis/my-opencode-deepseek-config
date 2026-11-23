# -*- coding: utf-8 -*-
"""footageqa.py — 素材剪辑生产线的专属门禁（F1..F9）。

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


def _stable(value):
    """把契约值归一成"同一份内容算同一个哈希"的形态。

    为什么要递归归一：
      * JSON 里 1 与 1.0、0.3 与 0.30000000000000004（浮点累加）都是"人眼一样"的值，
        序列化字符串却不同；不过一手就会让没改过的工程"旧签核突然失效"。
        浮点统一 round(…, 6) 后转字符串。
      * dict 交给 json.dumps(sort_keys=True) 排序；list 保持顺序——lines / shots 的
        先后本身就是内容（决定画面与时间轴），不能被排序吃掉。
      * 只做数值归一，不做类型强转：None / "" / False 各代表不同语义，不能合并。
    """
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return repr(round(float(value), 6))
    if value is None:
        return None
    return str(value)


def _fingerprint(foot, edl):
    """签核指纹：契约 + EDL 的内容哈希。改内容 -> 旧签核自动失效。

    覆盖范围 = 会改变成片观感的全部"声明值"，**不包括文件路径与 url**
    （换机器、换素材目录不该让签核失效）：title / treatment / subtitle /
    video（fps 与分辨率）/ audio（voice、rate、pitch、三路增益、music 与
    music_file）/ pacing（lead/tail/hold/outro，直接决定镜头长短与片尾）/
    credit（片尾出处卡）/ sources（license_note、burned_subs、watermark——它们
    决定 F2/F3 的判定）/ lines（id、text、sub、shot、hold）/ EDL 的 shots。

    反过来说，下面任何一类改动都会让 work/footage-signoff.json 里的旧签核
    **自动失效**（F7 变 skipped 并写明"内容已变"），必须重新看片：
      * 改台词 / 上屏字幕 / 镜头指向 / 镜头长度 / 每句 hold；
      * 改画幅 fps 与分辨率、改字幕字号与边距；
      * 改旁白声线/语速/音高、改环境声/配乐增益，或换配乐文件与曲风参数；
      * 改 lead / tail / outro（镜头与片尾长度全会变）；
      * 改片尾出处卡文字或出现时间；
      * 改素材出处说明，或改字幕带 / 水印框声明（F2/F3 结论会变）。
    """
    sources = []
    for s in foot.get("sources") or []:
        sources.append({"id": s.get("id"),
                        "license_note": s.get("license_note"),
                        "burned_subs": s.get("burned_subs"),
                        "watermark": s.get("watermark"),
                        "kind": s.get("kind")})
    audio = foot.get("audio") or {}
    payload = {"title": foot.get("title"),
               "treatment": foot.get("treatment"),
               "subtitle": foot.get("subtitle"),
               "video": foot.get("video"),
               "audio": {"voice": audio.get("voice"), "rate": audio.get("rate"),
                         "pitch": audio.get("pitch"),
                         "voice_gain": audio.get("voice_gain"),
                         "env_gain": audio.get("env_gain"),
                         "music_gain": audio.get("music_gain"),
                         "music_file": audio.get("music_file"),
                         "music": audio.get("music")},
               "pacing": foot.get("pacing"),
               "credit": foot.get("credit"),
               "licensing": foot.get("licensing"),
               "sources": sources,
               "lines": [{k: v for k, v in ln.items()
                          if k in ("id", "text", "sub", "shot", "hold")}
                         for ln in foot.get("lines") or []],
               "shots": [[s.get("id"), s.get("source"), s.get("at"), s.get("len")]
                         for s in (edl or {}).get("shots") or []]}
    h = hashlib.sha1()
    h.update(json.dumps(_stable(payload), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8"))
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
    # 覆盖度：check_edl 只在"源时长已知"时才判越界。sources.json 缺失、或某个源没探到
    # dur（探测失败 / 换过路径）时那条越界判定根本没执行，记 pass 就是"没查被当成通过"。
    # 图片源没有"源内时间码"语义（静帧循环铺满本镜），不参与越界判定。
    srcs = footagekit.source_map(foot)
    unknown = []
    for sid in sorted({str(s.get("source") or "") for s in shots}):
        rec = srcs_info.get(sid) or {}
        src = srcs.get(sid) or {}
        suffix = Path(str(src.get("path") or "")).suffix.lower()
        if rec.get("is_image") or suffix in footagekit.IMAGE_EXT:
            continue
        if not rec.get("dur"):
            unknown.append(sid)
    if unknown:
        return dict(gate="F1", status="skipped",
                    detail="缺源时长（work/sources.json 里没有 %s 的 dur），越界判定没执行："
                           "先跑 --stage probe 再重跑 qa（图片源不参与越界判定）"
                           % "、".join(unknown[:4]),
                    values={"unknown_duration": unknown, "shots": len(shots)})
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
    problems, bad_decl, checked = [], [], 0
    for sid, s in srcs.items():
        band = _declared_band(s, "burned_subs")
        if not band:
            if s.get("burned_subs"):
                # 有声明但解析不出 band：不能当成"没声明"混过去，否则下面的 pass 会谎称查过
                bad_decl.append(sid)
            continue
        checked += 1
        if box is None:
            continue                    # 可见区未知：交给下面的 skipped 分支
        rec = _overlaps(box, band, "burned_subs")
        if rec is None:
            bad_decl.append(sid)        # 声明区间非法（y1<=y0 / 非数字）
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
    if bad_decl:
        return dict(gate="F2", status="fail",
                    detail="这些源的 burned_subs 声明无法用于几何判定：%s"
                           "（需要 {y0,y1} 且 y1>y0，或 [y0,y1]）"
                           % "、".join(bad_decl[:4]),
                    values={"bad_declaration": bad_decl, "visible_box": box})
    if not checked:
        return dict(gate="F2", status="skipped",
                    detail="没有源声明 burned_subs（字幕带位置）；无法判定是否治理干净。"
                           "用 --stage probe 的建议值填进 sources[].burned_subs 后重跑",
                    values={"bright_suspicion": suspicion})
    return dict(gate="F2", status="pass",
                detail="%d/%d 个源声明了烧录字幕带，且都在 crop 之外（可见区 y=%.0f..%.0f）"
                       % (checked, len(srcs), box[1], box[3]),
                values={"visible_box": box, "bright_suspicion": suspicion,
                        "declared": checked, "sources": len(srcs)})


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
    problems, bad_decl, checked = [], [], 0
    for sid, s in srcs.items():
        band = _declared_band(s, "watermark")
        if not band:
            if s.get("watermark"):
                bad_decl.append(sid)    # 有声明但解析不出来：不能当成"没声明"
            continue
        checked += 1
        if box is None:
            continue                    # 可见区未知：交给下面的 skipped 分支
        rec = _overlaps(box, band, "watermark")
        if rec is None:
            bad_decl.append(sid)        # 声明区间非法（w/h<=0 / 非数字）
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
    if bad_decl:
        return dict(gate="F3", status="fail",
                    detail="这些源的 watermark 声明无法用于几何判定：%s"
                           "（需要 {x,y,w,h} 且 w,h>0，或 [x,y]）"
                           % "、".join(bad_decl[:4]),
                    values={"bad_declaration": bad_decl, "visible_box": box})
    if not checked:
        return dict(gate="F3", status="skipped",
                    detail="没有源声明 watermark（水印框）；无法判定水印是否去掉。"
                           "用 --stage probe 的建议值填进 sources[].watermark 后重跑",
                    values={"visible_box": box})
    return dict(gate="F3", status="pass",
                detail="%d/%d 个源声明了水印框，且都在 crop 之外（可见区 x=%.0f..%.0f y=%.0f..%.0f）"
                       % (checked, len(srcs), box[0], box[2], box[1], box[3]),
                values={"visible_box": box, "declared": checked, "sources": len(srcs)})


# ---------------------------------------------------------------- F4
def f4_av_equal(foot, work, out, tl, edl, ctx):
    # "成片不存在"不是"没执行"，是**执行了但没有可交付物**：直接调 run_all
    # （自检 / 第三方脚本 / 上一阶段失败后重跑 qa）时若记 skipped，报告会看起来
    # "这条门禁没查"而不是"成片没有"，正是本仓库反复强调的假绿形态。
    if not out or not Path(str(out)).is_file():
        return dict(gate="F4", status="fail",
                    detail="成片不存在：%s（先跑 --stage assemble；成片没产出时音画等长无从谈起）"
                           % (out,),
                    values={"out": str(out)})
    raw = filmkit.probe(str(out)) or {}
    streams = raw.get("streams") or []
    if not streams:
        return dict(gate="F4", status="fail",
                    detail="成片探测不到任何流（ffprobe 无输出）：%s（文件损坏 / 0 字节？）" % out,
                    values={"out": str(out), "streams": 0})
    # 流"少了一路"和流"读不出时长"是两件事：素材线的成片必须有画面+音轨，
    # 少一路的成片不能交付。原来用 format.duration 给缺失的那一路兜底，
    # 于是"只有视频流"的成片被算成 video==audio==fmt_dur 直接 pass（实测）。
    has_v = any(s.get("codec_type") == "video" for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    if not has_v or not has_a:
        lacking = "、".join([n for n, ok in (("视频流", has_v), ("音频流", has_a)) if not ok])
        return dict(gate="F4", status="fail",
                    detail="成片缺%s：%s（音画等长无从谈起；只有一路流的成片不能交付）"
                           % (lacking, out),
                    values={"out": str(out), "has_video": has_v, "has_audio": has_a})
    vdur = adur = None
    for s in streams:
        if s.get("codec_type") == "video" and s.get("duration"):
            vdur = float(s["duration"])
        elif s.get("codec_type") == "audio" and s.get("duration"):
            adur = float(s["duration"])
    # 流都在但某一路的 duration 字段缺失时，才用容器时长兜底（既不是"缺流"，也不是"读不出"）。
    fmt_dur = float((raw.get("format") or {}).get("duration") or 0.0)
    if vdur is None:
        vdur = fmt_dur or None
    if adur is None:
        adur = fmt_dur or None
    if not vdur or not adur:
        # 流在但时长读不出（容器缺 duration / 流被截断）：音画等长判不了，
        # 和"没有成片"一样不可交付，记 fail 并点名缺的是哪一路。
        lacking = "、".join([n for n, v in (("视频流", vdur), ("音频流", adur)) if not v])
        return dict(gate="F4", status="fail",
                    detail="成片读不到%s时长：%s（容器缺 duration 或流被截断）" % (lacking, out),
                    values={"out": str(out), "video": vdur, "audio": adur})
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
    total = a.size / float(sr)
    chans, gaps = [], []
    prev = 0.0
    for r in tl.get("lines") or []:
        s0, s1 = int(float(r["start"]) * sr), int(float(r["end"]) * sr)
        if s1 > s0:
            chans.append(a[s0:min(s1, a.size)])
        # 间隙 = 本镜窗口里**没有旁白**的部分（lead / hold / tail），不是"相邻两句之间"：
        # 时间轴按定义首尾相接（next.shot_start == prev.shot_end），那种间隙恒为 0 秒，
        # 于是 F6 在任何正常片子上都只会 skipped——门禁等于没执行（实测全链路跑完永远 skipped）。
        for g0, g1 in ((prev, float(r.get("shot_start") or 0.0)),
                       (float(r.get("end") or 0.0), float(r.get("shot_end") or 0.0))):
            a0, a1 = int(round(max(0.0, g0) * sr)), int(round(min(total, g1) * sr))
            # 0.25s 起算：默认 lead=0.35 / tail=0.55、模板 pacing 也一样，但项目里
            # 常把 tail 调到 0.4（自检就是这样），门槛写 0.5s 会让正常的尾巴全被丢掉。
            if a1 - a0 > sr // 4:
                gaps.append(a[a0:a1])
        prev = float(r.get("shot_end") or 0.0)
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


# ---------------------------------------------------------------- F9
def f9_subtitle_fit(foot, work, out, tl, edl, ctx):
    """字幕排版门禁：每行像素宽不超安全宽、总行数 <= 2、不越底边。

    为什么要它：素材线的字幕走 ASS 烧录（libass），**不是** PIL 的 draw_subtitle，
    所以 filmkit.qa_text_safe 管不到它；而 ASS 头是 WrapStyle:2（不自动折行），
    一句长旁白会直接横穿画面。实测 DEMO 二/三 29 句里有一半超宽，撞上叠加层，
    而当时 F1..F8 全部 pass（F9 就是为了补上这个渲染分支才加的）。
    """
    try:
        ass_path = Path(work) / "sub.ass"
    except Exception:                                          # noqa: BLE001
        ass_path = None
    if ass_path is None or not ass_path.exists():
        return dict(gate="F9", status="skipped",
                    detail="没有 work/sub.ass（字幕还没烧或不是 ASS 路线），本片没查")
    video = foot.get("video") or {}
    w = int(video.get("width") or 1920)
    h = int(video.get("height") or 1080)
    subcfg = foot.get("subtitle") or {}
    size = int(subcfg.get("size", 46))
    ml = int(subcfg.get("margin_l", 110))
    mr = int(subcfg.get("margin_r", 110))
    # 与写入端（write_ass）同一把尺子：那边 max_w = max(240, w - ml - mr)。
    # 只写 w-ml-mr 的话，边距很大时写入端按 240 折行、门禁按更小宽度量，会把每条都判超宽。
    max_w = max(240, w - ml - mr)
    try:
        import filmkit as _K
        tw = _K.text_w
        _fam = (subcfg.get("family") or "sans")
        # 字号必须从 **ASS 文件里的 Style 段**读，不能从 footage.json 的 subtitle.size 读。
        # 写端为了放下长句会**降字号**（实测 Main 从 44 降到 40），Style 里写的是降后的字号；
        # 若门禁按 44 量，就会把每一行都判成超宽（实测连报 18 行）。
        _styles, _margins = {}, {}
        # 列号从 Format 行读，不写死下标：Fontname 里带逗号、或写入端调整列序时，
        # 死下标会把 MarginV 读成别的字段（门禁按错的值判"越底边"）。
        _cols = {"Name": 0, "Fontsize": 2, "MarginV": 21}
        for _ln in ass_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if _ln.startswith("Format:") and "," in _ln:
                _fmt = [x.strip() for x in _ln[len("Format:"):].split(",")]
                _cols = {name: i for i, name in enumerate(_fmt)}
                continue
            if _ln.startswith("Style:") and "," in _ln:
                _p = _ln.split(",")
                try:
                    _col_name, _col_size = _cols["Name"], _cols["Fontsize"]
                    _col_mv = _cols.get("MarginV", 21)
                    _name = _p[_col_name].split()[-1]
                    _styles[_name] = int(_p[_col_size])
                    _margins[_name] = int(_p[_col_mv]) if len(_p) > _col_mv else 0
                except (IndexError, ValueError, KeyError):
                    pass
        if not _styles:
            _styles = {"Main": size, "Note": int(subcfg.get("note_size", max(24, int(size * 0.62))))}
            _mv = int(subcfg.get("margin_v", 38))
            _margins = {"Main": _mv, "Note": _mv}
        fonts = {k: _K.font(v, "reg", _fam) for k, v in _styles.items()}
        max_w = max(240, int(w) - ml - mr)
    except Exception:                                          # noqa: BLE001
        return dict(gate="F9", status="skipped",
                    detail="没有 filmkit（量不了像素宽），本片没查")
    bad_w, bad_rows, bad_bottom = [], [], []
    rows = 0
    for line in ass_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        txt = parts[9].replace("\\N", "\n").replace("\\n", "\n")
        rows += 1
        segs = [x for x in txt.split("\n") if x.strip()]
        if len(segs) > 2:
            bad_rows.append((parts[1], len(segs), txt[:18]))
        _style = (parts[3] or "Main").strip()
        _font = fonts.get(_style) or fonts["Main"]
        for seg in segs:
            wpx = tw(seg, _font)
            if wpx > max_w:
                bad_w.append((parts[1], int(wpx), max_w, seg[:18]))
        # 竖直方向：Alignment=2（底部居中）+ MarginV，行数一多就往上长；长到画布外就是出画。
        # Style 里没有行距字段，按 ASS 实践用 1.2*字号 估每行高度，只拦明显越界的情况。
        _size = _styles.get(_style) or _styles.get("Main") or size
        _mv = int(_margins.get(_style, 0))
        if segs and _mv + len(segs) * _size * 1.2 > h:
            bad_bottom.append((parts[1], _mv, len(segs), _size))
    if not rows:
        return dict(gate="F9", status="skipped", detail="sub.ass 里没有字幕行")
    if bad_w or bad_rows or bad_bottom:
        det = []
        if bad_w:
            det.append("%d 行超宽（最宽 %dpx > %dpx）：%s"
                       % (len(bad_w), max(b[1] for b in bad_w), max_w,
                          "；".join("%s %r" % (b[0], b[3]) for b in bad_w[:3])))
        if bad_rows:
            det.append("%d 条超过 2 行：%s" % (len(bad_rows),
                                              "；".join("%s=%d行" % (b[0], b[1]) for b in bad_rows[:3])))
        if bad_bottom:
            det.append("%d 条越出底边（MarginV %d + %d 行 x %dpx > 画布 %dpx）：%s"
                       % (len(bad_bottom), bad_bottom[0][1], bad_bottom[0][2],
                          bad_bottom[0][3], h,
                          "；".join(b[0] for b in bad_bottom[:3])))
        return dict(gate="F9", status="fail", detail="；".join(det),
                    values={"too_wide": len(bad_w), "too_many_rows": len(bad_rows),
                            "too_low": len(bad_bottom), "max_w_px": max_w,
                            "canvas_h": h})
    return dict(gate="F9", status="pass",
                detail="%d 条字幕：每行 <= %dpx、行数 <= 2、未越底边（写入端 write_ass 全局选字号后折行）"
                       % (rows, max_w),
                values={"rows": rows, "max_w_px": max_w, "canvas_h": h})


GATES = [f1_edl, f2_subtitle_bleed, f3_watermark, f4_av_equal,
         f5_provenance, f6_voice_dominance, f7_signoff, f8_source_variety,
         f9_subtitle_fit]


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
    """跑 F1..F9 + 通用质检，写 work/footage-gates.json 与 out/QC-REPORT-footage.md。"""
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
