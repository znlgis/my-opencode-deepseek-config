# -*- coding: utf-8 -*-
"""matchstyle.py — 让"自己画"的那几段向素材的整体风格看齐（hybrid 专用）

为什么要有它
------------
hybrid（素材段 + 图文段）最容易出问题的地方不是代码，是**外观断层**：
素材段的颜色由素材自己决定，图文段的底色/主色由 design.json 决定，两边各走各的。
实测观感是"前一段是纪录片，后一段是 PPT"，或者更糟——有人为了让它们统一，
在素材上叠一层主题色，素材立刻发灰、发闷、细节像蒙了雾（那不是统一，是糊）。

正确的做法是反过来：**素材保持原色，把图文段的底色/主色锚到素材上**。
本脚本只干这一件事：
  1) 按 EDL 实际用到的镜头抽帧（没有 EDL 就在片子里均匀抽）；
  2) 统计这些画面的色相 / 饱和度 / 明度 / 明暗（中位数，不是平均值）；
  3) 把统计值喂给 designkit 的配色求解器，得到一套**与素材同族**的 11 色调色板；
  4) --apply 时写进 work/design.json 的 visual.palette，并把 footage.json 的
     treatment.letterbox_color 调成同族近黑（两段黑边在拼接处不跳）。

为什么不自己写一套配色算法：designkit._solve_palette 已经负责"语义色必须过对比度门槛"
（ink/muted/dim/accent 对背景、good/bad 对卡片），theme_lint 是同一份判据。
颜色数学抄一份到本脚本，就等于养出第二个真相 —— 迟早两边给出不同的"合格"。

用法
----
    python matchstyle.py --footage footage.json --work work --report      # 只看画像（只写 work/style_profile.json，不改 design/footage）
    python matchstyle.py --footage footage.json --work work --apply       # 写 design.json + footage.json

    --align-bars   连遮幅几何一起对齐（改 treatment.scale，让素材黑边高度接近自绘段）
    --sat-damp    底色吃素材饱和度的比例（默认 0.6；太花就调小，太闷就调大）
    --json        只输出 JSON（给 agent 解析用）

退出码
------
    0  成功（--report 一律 0；--apply 只有真的写成功才 0）
    1  抽帧/统计失败（ffmpeg 不在、素材解不开、一帧都抽不到）
    2  前置条件不满足（缺 footage.json / design.json、配色无解）——**不改任何文件**

与其它脚本的关系
----------------
* 不读 proposal.json、不改提案台账：它只改 design.json 的调色板这一块。
* 改了 design.json 就改了设计指纹，propose.py --adopt 需要重跑一次（脚本会打印命令）。
* 素材段的出片流程完全不变：本脚本不改任何滤镜链，只改一个颜色字段（外加可选 scale）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 与 assets/scenes.py 的 _letterbox_overlay 同源：那里 top/bottom 默认就是 40/44，
# 写死在这里等于"两段遮幅对齐"的参照系只有一个（改了 scenes 要一起改）。
BARS_DEFAULT = {"top": 40, "bottom": 44}

SCHEMA = 1
OUT_W, OUT_H = 160, 90          # 抽样分辨率：只用来取颜色分布，不追求清晰
_TMP_SEQ = [0]                  # 原子写的临时文件序号（见 _write_json_atomic）
V_FLOOR, V_CEIL = 0.05, 0.97    # 纯黑黑边与过曝高光不参与统计
S_FLOOR = 0.06                  # 近灰像素的"色相"没有意义，权重记 0


def log(msg):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


# ============================================================================
# 1. 读写与前置条件
# ============================================================================

def _load_json(path, default=None):
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _load_json_status(path):
    """-> (data, status)，status ∈ {"ok", "absent", "broken"}。

    坏 JSON 不能当成"没有这个文件"：前者要修，后者补文件就行；混成一句
    "没有 work/edl.json" 会让人去创建一份本来就存在（只是写坏了）的文件。
    """
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            return json.load(fh), "ok"
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError):
        return None, "broken"


def _write_json_atomic(path, data):
    """原子写：先写同目录临时文件再替换，半截文件永远不会出现在盘上。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 临时名必须唯一：固定 ".tmp" 时两个并发 --apply 会互相截断对方的临时文件。
    _TMP_SEQ[0] += 1
    tmp = p.with_suffix("%s.tmp-%d-%d" % (p.suffix, os.getpid(), _TMP_SEQ[0]))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))


def _backup(path, work):
    """覆盖前留一份：回滚靠它，不靠"我记得原来是什么"。"""
    src = Path(path)
    if not src.is_file():
        return ""
    dst = Path(work) / "backup" / ("%s.before-matchstyle.json" % src.stem)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(dst))
    return str(dst)


def _modules():
    """就近导入（与其它脚本同口径：缺模块时报一句人话，不抛 ImportError 栈）。"""
    try:
        import designkit
        import filmkit
        import footagekit
        import stylekit
    except Exception as e:                                   # noqa: BLE001
        raise SystemExit("matchstyle 需要 designkit / filmkit / footagekit / stylekit：%s"
                         % e) from e
    return designkit, filmkit, footagekit, stylekit


# ============================================================================
# 2. 抽帧：只抽 EDL 里真的会用到的画面
# ============================================================================

def _crop_of(treatment):
    """treatment.crop "w:h:x:y" -> (w,h,x,y)；没有就 None。"""
    raw = str((treatment or {}).get("crop") or "").strip()
    if not raw:
        return None
    try:
        parts = [int(x) for x in raw.split(":")[:4]]
    except ValueError:
        return None
    if len(parts) != 4 or min(parts) < 0 or parts[0] < 2 or parts[1] < 2:
        return None
    return tuple(parts)


def _times_of(footage, edl, per_source):
    """每个 source 要抽的秒数列表。

    优先用 EDL：只抽**成片里真的会出现**的镜头（at + len 的 35% / 75%）。
    为什么不做全片均匀抽样：素材里常常有大量没用上的段落（黑场、片头字幕、
    与文案无关的镜头），把它们算进"整体风格"会把配色拉向一个片子里根本没有的气质。
    """
    footagekit = _modules()[2]
    # --n-frames 0 会让下面的 len(times)/per_source 除零（CLI 的 type=int 不挡 0）。
    per_source = max(1, int(per_source))
    srcs = footagekit.source_map(footage)
    order = [str(s["id"]) for s in footage.get("sources") or []]
    out = {sid: [] for sid in order}

    for s in (edl or {}).get("shots") or []:
        sid = str(s.get("source") or "")
        if sid not in out:
            continue
        try:
            at = float(s.get("at") or 0.0)
            ln = float(s.get("len") or 0.0)
        except (TypeError, ValueError):
            continue
        if ln <= 0.05:
            continue
        for frac in (0.35, 0.75):
            t = round(at + ln * frac, 2)
            if t >= 0:
                out[sid].append(t)

    for sid in order:
        times = sorted(set(out[sid]))
        if times:
            # 同一镜头抽两点；镜头多时按时间均匀裁剪到 per_source 个（保持先后顺序）
            if len(times) > per_source:
                step = len(times) / float(per_source)
                times = [times[min(len(times) - 1, int(i * step))] for i in range(per_source)]
            out[sid] = times
            continue
        # 没有 EDL（或这条线还没排轴）：退回"片子里均匀抽"，并在报告里说明来源
        src = srcs[sid]
        try:
            info = footagekit.probe_file(footagekit.source_path(footage, src))
        except SystemExit:
            out[sid] = []
            continue
        dur = float(info.get("dur") or 0.0)
        if dur <= 0.2:
            out[sid] = [0.0]
            continue
        n = max(3, int(per_source))
        out[sid] = [round(dur * (0.06 + 0.88 * i / (n - 1)), 2) for i in range(n)]
    return out


def _grab(src_path, t, crop, color_eq, filmkit):
    """抽一帧并缩到 160x90，返回 RGB 原始字节；抽不到返回 None。"""
    vf = []
    if crop:
        vf.append("crop=%d:%d:%d:%d" % crop)
    vf.append("scale=%d:%d:flags=area" % (OUT_W, OUT_H))
    if color_eq:
        vf.append(color_eq)          # 素材段真的会吃的调色，抽样必须跟着吃
    cmd = [filmkit.FFMPEG, "-v", "error", "-ss", "%.3f" % max(0.0, t),
           "-i", str(src_path), "-frames:v", "1", "-vf", ",".join(vf),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0 or len(p.stdout) < OUT_W * OUT_H * 3:
        if color_eq:
            # 调色串写坏时不要把整次采样判死：退回不调色的画面。
            return _grab(src_path, t, crop, "", filmkit)
        return None
    return p.stdout[:OUT_W * OUT_H * 3]

# ============================================================================
# 3. 统计：中位数 + 圆均值，不用算术平均
# ============================================================================

def _luma(r, g, b):
    """sRGB 相对亮度（同一份 0..1 公式，供"明暗族"判定用）。"""
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _frame_stats(data):
    """单帧 -> {hue, sat, val, luma, contrast, hue_spread, n}。

    为什么用中位数：一帧里只要有一块大面积的黑场或天空，算术平均就被拽走；
    中位数回答的是"这一帧**典型**是什么颜色"，那才是观众记住的观感。
    """
    import numpy as np
    from PIL import Image

    img = Image.frombytes("RGB", (OUT_W, OUT_H), data)
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32)
    hue = hsv[:, :, 0] * (360.0 / 255.0)
    sat = hsv[:, :, 1] / 255.0
    val = hsv[:, :, 2] / 255.0
    rgb = np.asarray(img, dtype=np.float32)

    mask = (sat >= S_FLOOR) & (val >= V_FLOOR) & (val <= V_CEIL)
    if mask.sum() < mask.size * 0.05:                 # 近灰画面：退回全部像素
        mask = np.ones(mask.shape, dtype=bool)
    h = hue[mask]
    s = sat[mask]
    v = val[mask]
    r = float(np.median(rgb[:, :, 0][mask]))
    g = float(np.median(rgb[:, :, 1][mask]))
    b = float(np.median(rgb[:, :, 2][mask]))

    # 颜色权重 = 饱和度：近灰像素的色相是噪声，不该参与"主色相"
    w = s.astype(np.float64)
    ang = np.radians(h.astype(np.float64))
    sx = float((np.cos(ang) * w).sum())
    sy = float((np.sin(ang) * w).sum())
    if abs(sx) + abs(sy) < 1e-6:
        main_hue = float(np.median(h)) if h.size else 0.0
        spread = 0.0
    else:
        main_hue = float(np.degrees(np.arctan2(sy, sx))) % 360.0
        d = (h.astype(np.float64) - main_hue + 180.0) % 360.0 - 180.0
        spread = float(np.percentile(np.abs(d), 75))
    return {"hue": main_hue, "sat": float(np.median(s)), "val": float(np.median(v)),
            "luma": _luma(r, g, b), "contrast": float(np.std(v)),
            "hue_spread": spread, "n": int(h.size)}


def _aggregate(rows):
    """多帧 -> 一个风格画像。色相按饱和度加权做圆均值，其余取中位数。"""
    import math

    if not rows:
        raise RuntimeError("一帧都没抽到（素材不存在 / ffmpeg 解不开 / 时间点越界）")
    sx = sy = 0.0
    for r in rows:
        w = max(1e-3, float(r["sat"]))
        a = math.radians(float(r["hue"]))
        sx += math.cos(a) * w
        sy += math.sin(a) * w
    hue = (math.degrees(math.atan2(sy, sx)) % 360.0) if (abs(sx) + abs(sy) > 1e-9) else 0.0
    return {
        "hue": round(hue, 1),
        "sat": round(statistics.median([r["sat"] for r in rows]), 4),
        "val": round(statistics.median([r["val"] for r in rows]), 4),
        "luma": round(statistics.median([r["luma"] for r in rows]), 4),
        "contrast": round(statistics.median([r["contrast"] for r in rows]), 4),
        "hue_spread": round(statistics.median([r["hue_spread"] for r in rows]), 1),
        "frames": len(rows),
    }


def measure(footage, edl, work, per_source=9):
    """抽帧 + 统计。返回 (profile, rows)：profile 只放事实，不放"要不要改"的判断。"""
    designkit, filmkit, footagekit, stylekit = _modules()
    srcs = footagekit.source_map(footage)
    tr = footage.get("treatment") or {}
    crop = _crop_of(tr)
    color_eq = str(tr.get("color") or "").strip()
    times = _times_of(footage, edl, per_source)
    rows, used = [], {}
    from_edl = bool((edl or {}).get("shots"))
    for sid, ts in times.items():
        if sid not in srcs:
            continue
        path = footagekit.source_path(footage, srcs[sid])
        if not Path(path).exists():
            used[sid] = {"frames": 0, "missing": True, "path": str(path)}
            continue
        got = 0
        for t in ts:
            data = _grab(path, t, crop, color_eq, filmkit)
            if data is None:
                continue
            rows.append(_frame_stats(data))
            got += 1
        used[sid] = {"frames": got, "path": str(path),
                     "crop": ("%d:%d:%d:%d" % crop) if crop else ""}
    if not rows:
        missing = [k for k, v in used.items() if v.get("missing")]
        raise RuntimeError("抽帧全失败（%d 个源缺文件：%s）"
                           % (len(missing), ", ".join(missing) or "无"))
    prof = _aggregate(rows)
    prof["sources"] = used
    prof["sampling"] = {"from": "edl" if from_edl else "uniform",
                        "color_filter": color_eq,
                        "crop": ("%d:%d:%d:%d" % crop) if crop else "",
                        "edl_status": "ok" if from_edl else "absent"}
    return prof, rows


# ============================================================================
# 4. 由画像求调色板（复用 designkit 的求解器，不自己配色）
# ============================================================================

def palette_for(prof, sat_damp=0.6):
    """画像 -> (palette, family)。明暗先定，再把素材的 h/s/v **映射进** designkit 的取值空间。

    规则只有三条，都是"为什么"级别的：
      1) 明暗跟素材走：luma < 0.35 记暗底（与 stylekit.is_dark 同一判据）——
         暗素材配浅底会像"两部片拼在一起"；
      2) 色相全收：素材偏暖就出暖底，偏冷就出冷底，"同族"就是这么来的；
      3) 饱和度必须打折：素材里一块高饱和的招牌路牌，铺满整屏就成了荧光板。
         打折比例是参数（--sat-damp），不是写死的审美。
    求解失败时逐档退让（降饱和 -> 取空间中值 -> 熄灭饱和），而不是回落到某个固定配色。
    """
    designkit = _modules()[0]
    dark = float(prof["luma"]) < 0.35
    space = designkit._PALETTE_SPACE[bool(dark)]
    s_lo, s_hi = float(space["s"][0]), float(space["s"][1])
    v_lo, v_hi = float(space["v"][0]), float(space["v"][1])
    hue = float(prof["hue"])

    def _clamp(x, lo, hi):
        return lo if x < lo else (hi if x > hi else x)

    sat = _clamp(float(prof["sat"]) * float(sat_damp), s_lo, s_hi)
    val = _clamp(v_lo + float(prof["val"]) * (v_hi - v_lo), v_lo, v_hi)
    cands = [(sat, val, "按素材落点"),
             (sat * 0.7, val, "饱和降一档"),
             ((s_lo + s_hi) / 2.0, (v_lo + v_hi) / 2.0, "取空间中值"),
             (s_lo, _clamp(val, v_lo, v_hi), "熄灭饱和")]
    last = None
    for s, v, note in cands:
        try:
            res = designkit._solve_palette_retry((hue, s, v, dark))
        except ValueError as e:                              # 语义色对比度不可达
            last = e
            continue
        fam = res["family"]
        return res["palette"], {"hue": round(float(fam[0]), 1),
                                "sat": round(float(fam[1]), 4),
                                "val": round(float(fam[2]), 4),
                                "dark": bool(fam[3]),
                                "shift": round(float(res.get("shift") or 0.0), 1),
                                "note": note}
    raise RuntimeError("这套素材的颜色解不出可用配色（最后一档：%s）" % last)


def _hue_name(hue):
    """最近的中文锚点名（只给人看，不参与任何计算）。"""
    try:
        designkit = _modules()[0]
    except SystemExit:
        return ""
    best = None
    for name, (h, _dark) in designkit._PALETTE_ANCHOR.items():
        d = abs((float(hue) - float(h)) % 360.0)
        d = d if d <= 180.0 else 360.0 - d
        if best is None or d < best[0]:
            best = (d, name)
    if best and best[0] <= designkit._HUE_WINDOW:
        return "%s（%.0f°）" % (best[1], hue)
    return ""

# ============================================================================
# 5. 遮幅：自绘段 40/44px 黑边，素材段是 pad 出来的居中黑边
# ============================================================================

def _parse_filter(vf):
    """从 ffmpeg 滤镜串里读 scale 与 pad（读的是**成片真的会用的那条**）。"""
    out = {"scale": None, "pad": None}
    for part in str(vf or "").split(","):
        if part.startswith("scale="):
            nums = part[len("scale="):].split(":")[:2]
            try:
                out["scale"] = (int(nums[0]), int(nums[1]))
            except (ValueError, IndexError):
                pass
        elif part.startswith("pad="):
            body = part[len("pad="):]
            color = ""
            if ":color=" in body:
                body, color = body.split(":color=", 1)
            nums = body.split(":")
            try:
                out["pad"] = {"y": int(nums[3]) if len(nums) > 3 else 0,
                              "color": color or ""}
            except ValueError:
                pass
    return out


def letterbox_of(footage, design):
    """两段的遮幅几何/颜色对照 + 是否一致（None = 自绘段没有遮幅，无从对齐）。"""
    footagekit = _modules()[2]
    tr = footage.get("treatment") or {}
    video = footage.get("video") or {}
    vh = int(video.get("height") or 1080)
    parsed = _parse_filter(footagekit.build_treatment_filter(tr, video))
    foot = {"scale": parsed["scale"],
            "color": parsed["pad"]["color"] if parsed["pad"] else ""}
    if parsed["scale"] and parsed["pad"]:
        sh = int(parsed["scale"][1])
        y = int(parsed["pad"]["y"])
        foot.update({"top": y, "bottom": max(0, vh - y - sh), "visible_h": sh})
    else:
        foot.update({"top": 0, "bottom": 0, "visible_h": vh})

    motif = ((design or {}).get("visual") or {}).get("motif") or {}
    draw = None
    if str(motif.get("kind") or "") == "letterbox":
        det = motif.get("detail") or {}
        draw = {"top": int(det.get("top") or BARS_DEFAULT["top"]),
                "bottom": int(det.get("bottom") or BARS_DEFAULT["bottom"]),
                "color": [0, 0, 0]}
        draw["visible_h"] = vh - draw["top"] - draw["bottom"]

    info = {"draw": draw, "footage": foot}
    if draw:
        info["delta"] = {"top": foot["top"] - draw["top"],
                         "bottom": foot["bottom"] - draw["bottom"]}
        info["match"] = (abs(info["delta"]["top"]) <= 2
                         and abs(info["delta"]["bottom"]) <= 2)
    else:
        info["match"] = None
    return info


def _near_black(hue, sat):
    """同族近黑：色相跟素材走，饱和度与明度压到"看着就是黑"的档位。

    为什么不是直接用配色里的 bg：自绘段的黑边是**纯黑**（scenes 写死），
    素材段的 pad 若用浅色底，拼接处会从黑边直接跳到米色 —— 那就成"两部片"了。
    这里要的是"黑得跟主题一个色系"，不是"把主题色铺成黑边"。
    """
    stylekit = _modules()[3]
    return stylekit.hsv_to_rgb(float(hue) % 360.0,
                               max(0.0, min(0.35, float(sat) * 0.5)), 0.045)


def _hex_rgb(rgb):
    return "0x%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _align_scale(footage, design):
    """算"让素材黑边高度等于自绘段"的 (scale, crop, note)。

    为什么不能只改 scale：build_treatment_filter 的 pad 是**纵向居中**的
    （pad=w:h:0:y），按比例缩下来的片子若宽度不足 1920，右边会留一条黑带——
    上下对齐了，左右又冒出来（实测 640x360 素材对齐到 996 高时右侧出现 150px 黑带）。
    所以对齐画幅必须同时**收缩 crop**，让取景窗的宽高比刚好等于目标画幅，再缩放贴边。

    收缩只缩小取景窗，不会把已经裁掉的烧录字幕/水印放回来（那才是危险的方向）；
    代价是画面两侧（或上下）多裁掉几个百分点——note 里会说清楚裁了多少。
    """
    footagekit = _modules()[2]
    tr = footage.get("treatment") or {}
    video = footage.get("video") or {}
    vw = int(video.get("width") or 1920)
    vh = int(video.get("height") or 1080)
    crop = _crop_of(tr)
    if crop:
        cw, ch, cx, cy = crop[0], crop[1], crop[2], crop[3]
    else:
        cw, ch, cx, cy = vw, vh, 0, 0
        try:
            srcs = footagekit.source_map(footage)
            first = srcs[str(footage["sources"][0]["id"])]
            info = footagekit.probe_file(footagekit.source_path(footage, first))
            vid = (info.get("video") or [{}])[0]
            cw, ch = int(vid.get("w") or vw), int(vid.get("h") or vh)
        except Exception:                                   # noqa: BLE001
            pass
    motif = ((design or {}).get("visual") or {}).get("motif") or {}
    det = motif.get("detail") or {}
    if str(motif.get("kind") or "") == "letterbox":
        target = vh - int(det.get("top") or BARS_DEFAULT["top"]) \
            - int(det.get("bottom") or BARS_DEFAULT["bottom"])
    else:
        target = vh
    target = max(2, min(vh, target))
    target -= target % 2
    aspect = vw / float(target)

    # 先让取景窗的宽高比等于目标画幅（只缩不放），再缩放贴边 —— 这样 pad 只会在上下补边。
    new_cw, new_ch = cw, ch
    if cw / float(max(1, ch)) > aspect + 1e-3:
        new_cw = int(round(ch * aspect))
    elif cw / float(max(1, ch)) < aspect - 1e-3:
        new_ch = int(round(cw / aspect))
    new_cw = max(2, min(cw, new_cw - new_cw % 2))
    new_ch = max(2, min(ch, new_ch - new_ch % 2))
    crop_str = None
    note = ""
    if (new_cw, new_ch) != (cw, ch):
        nx = cx + (cw - new_cw) // 2
        ny = cy + (ch - new_ch) // 2
        crop_str = "%d:%d:%d:%d" % (new_cw, new_ch, nx, ny)
        lost = 1.0 - (new_cw * new_ch) / float(max(1, cw * ch))
        note = ("为对齐画幅多裁掉 %.1f%% 画面（取景窗 %dx%d -> %dx%d）；"
                "只看上下补边、不改画幅就别加 --align-bars"
                % (lost * 100.0, cw, ch, new_cw, new_ch))
    return "%d:%d" % (vw, target), crop_str, note


# ============================================================================
# 6. 落盘
# ============================================================================

def _apply_design(design, palette):
    """把调色板写进 design 的副本；顺手过一遍 design_to_plan（含 theme_lint）。

    为什么在写盘**之前**校验：调色板是逻辑后果（素材偏暖 -> 暖底），可能让 ink
    对背景的对比度掉到 AA 以下。写进去再发现，就是"渲完才发现字幕看不清"。
    """
    designkit = _modules()[0]
    clone = json.loads(json.dumps(design))
    clone.setdefault("visual", {})["palette"] = {
        k: [int(c[0]), int(c[1]), int(c[2])] for k, c in palette.items()}
    bad = designkit.validate_design(clone)
    if bad:
        raise RuntimeError("新调色板让设计没过校验：%s" % "；".join(bad[:3]))
    try:
        designkit.design_to_plan(clone)      # 连 theme_lint（对比度门槛）一起过
    except ValueError as e:
        raise RuntimeError("新调色板没通过 theme_lint：%s" % e) from e
    return clone


def _day_night_conflict(design, family):
    """设计原本偏亮、但素材把我们锚到暗底 —— 这是实测最贵的坑，必须显式说出来。

    背景：一支宋画/实景混剪片，选中了「明色」设计（浅底、明亮活泼），但素材实测
    luma=0.12（绢本暗调）。按 matchstyle 的规则把图文段配色锚到素材后，图文段变成
    近黑底（bg=[13,12,11]）—— 与用户当初选的"明色"完全相反，等于偷偷改了需求。

    判定：读 design.visual.palette 里 bg 的亮度；素材 family 给的明暗族；
    两者相反就警告。**只警告不阻止**：确实要暗调片子的人应该能继续走，
    但绝不能"套了才发现全片变黑"。
    """
    if not isinstance(design, dict):
        return None
    pal = (design.get("visual") or {}).get("palette") or {}
    bg = pal.get("bg")
    if not isinstance(bg, (list, tuple)) or len(bg) != 3:
        return None
    try:
        # 感知亮度（Rec.601）：0=黑 1=白
        bg_luma = (0.299 * float(bg[0]) + 0.587 * float(bg[1])
                   + 0.114 * float(bg[2])) / 255.0
    except (TypeError, ValueError):
        return None
    was_dark = bg_luma < 0.20
    will_dark = bool(family.get("dark"))
    if was_dark == will_dark:
        return None
    if was_dark and not will_dark:
        return ("设计原本是暗底（bg 亮度 %.2f），素材把配色锚到**亮底**："
                "原设计意图会被改写；若这不是你要的，先降自绘占比或换素材" % bg_luma)
    return ("设计原本是亮底（bg 亮度 %.2f），素材把配色锚到**暗底**：图文段会变成近黑，"
            "与「明亮」类设计意图相反。三条出路（按代价从低到高）："
            "1) 降低自绘占比：改成 compose.py 的「素材打底 + 小面积叠加」，几乎不需要配色（推荐）；"
            "2) 保留亮色设计，不用 --apply（素材段与图文段靠暗场过渡衔接）；"
            "3) 接受暗调，但先跟需求方确认——这不是调色问题，是改需求。"
            % bg_luma)


def _warnings_of(prof, bars, measured_luma=None, edl_status="absent"):
    out = []
    if prof["sampling"]["from"] == "uniform":
        # 坏 JSON 与"没有文件"要分开说：前者去修，后者去补，混成一句会让人补错文件。
        if edl_status == "broken":
            out.append("work/edl.json 存在但读不出（坏 JSON）：本次按全片均匀抽样"
                       "（会混进没用上的段落）；修好 EDL 再测更准")
        elif edl_status == "ok":
            out.append("work/edl.json 里没有 shots：按全片均匀抽样"
                       "（先跑 footage.py --stage edl 再测会更准）")
        else:
            out.append("没有 work/edl.json：按全片均匀抽样（会混进没用上的段落，"
                       "先跑 footage.py --stage edl 再测会更准）")
    if prof["hue_spread"] > 40.0:
        out.append("素材色相离散度 %.0f°（>40°）：素材本身色调就不统一，"
                   "先统一 treatment.color 再谈『向素材看齐』" % prof["hue_spread"])
    luma = prof["luma"] if measured_luma is None else measured_luma
    if luma > 0.6:
        out.append("素材偏亮（luma %.2f）：自绘段会是浅纸底，这是设计上的必然；"
                   "觉得反差大就把 --sat-damp 调小" % luma)
    foot = bars["footage"]
    if bars["draw"] is None:
        if foot["top"] or foot["bottom"]:
            out.append("自绘段没有遮幅（motif 不是 letterbox），素材段却有 %d/%dpx 黑边："
                       "拼接处画幅会跳。要么把 design.visual.motif.kind 改成 letterbox，"
                       "要么 --align-bars 让素材填满全高"
                       % (foot["top"], foot["bottom"]))
    elif bars["match"] is False:
        out.append("黑边高度差 %+d/%+dpx（素材 %d/%d vs 自绘 %d/%d）："
                   "≥3px 就看得出画幅跳，用 --align-bars 对齐"
                   % (bars["delta"]["top"], bars["delta"]["bottom"],
                      foot["top"], foot["bottom"],
                      bars["draw"]["top"], bars["draw"]["bottom"]))
    return out


def run(args):
    designkit, filmkit, footagekit, stylekit = _modules()
    work = Path(args.work)
    design_path = Path(args.design) if args.design else (work / "design.json")
    foot_path = Path(args.footage)
    if not foot_path.is_file():
        log("[matchstyle] 找不到 footage.json：%s（素材线的契约文件）" % foot_path)
        return 2
    footage = footagekit.load_footage(str(foot_path))
    edl, edl_status = _load_json_status(work / "edl.json")
    design, design_status = _load_json_status(design_path)
    if bool(args.apply) and design_status != "ok":
        # 前置条件不满足：退 2 且**零写入**。不能退 1——1 是"跑了但失败"，
        # 调用方（agent 或流水线）分不清"该去补设计"还是"素材坏了"。
        why = ("（文件存在但读不出：坏 JSON，先修好或重跑 --confirm）"
               if design_status == "broken" else "（先用 propose.py --confirm 冻结设计）")
        log("[matchstyle] --apply 需要可读的 %s%s" % (design_path, why))
        return 2

    prof, _rows = measure(footage, edl, work, per_source=args.n_frames)
    # 抽样来源是 measure 判的，但"EDL 到底是不在还是坏了"只有这里知道；写进画像以便复现。
    prof["sampling"]["edl_status"] = edl_status
    palette, family = palette_for(prof, sat_damp=args.sat_damp)
    bars = letterbox_of(footage, design)
    near_black = _near_black(family["hue"], family["sat"])
    prof["warnings"] = _warnings_of(prof, bars, edl_status=edl_status)
    if design_status == "broken":
        prof["warnings"].append("%s 存在但读不出（坏 JSON）：本次按\"没有设计\"处理，"
                                "遮幅对照不可用" % design_path)
    conflict = _day_night_conflict(design, family)
    if conflict:
        prof["warnings"].append(conflict)
        prof["day_night_conflict"] = True

    out = {
        "schema": SCHEMA,
        "footage": str(foot_path.resolve()),
        "design": str(design_path.resolve()) if isinstance(design, dict) else "",
        "measured": {k: prof[k] for k in
                     ("hue", "sat", "val", "luma", "contrast", "hue_spread", "frames")},
        "sampling": prof["sampling"],
        "sources": prof["sources"],
        "hue_named": _hue_name(prof["hue"]),
        "family": family,
        "palette": {k: [int(c[0]), int(c[1]), int(c[2])] for k, c in palette.items()},
        "letterbox": bars,
        "proposed": {"letterbox_color": _hex_rgb(near_black)},
        "applied": False,
        "warnings": prof["warnings"],
        "day_night_conflict": bool(prof.get("day_night_conflict")),
        "next": [],
    }
    if args.align_bars:
        scale, crop_str, note = _align_scale(footage, design)
        out["proposed"]["scale"] = scale
        if crop_str:
            out["proposed"]["crop"] = crop_str
        if note:
            out["warnings"].append(note)

    if args.apply:
        try:
            new_design = _apply_design(design, palette)
        except RuntimeError as e:
            out["error"] = str(e)
            _write_json_atomic(work / "style_profile.json", out)
            log("[matchstyle] %s" % e)
            log("           本次没有写任何文件（design.json / footage.json 保持原样）")
            return 2

        out["backups"] = {"design": _backup(design_path, work),
                          "footage": _backup(foot_path, work)}
        _write_json_atomic(design_path, new_design)

        new_foot = json.loads(json.dumps(footage))
        new_foot.pop("_base", None)
        tr = dict(new_foot.get("treatment") or {})
        tr["letterbox_color"] = out["proposed"]["letterbox_color"]
        if args.align_bars and out["proposed"].get("scale"):
            tr["scale"] = out["proposed"]["scale"]
            # 取景窗必须跟着改：只改 scale 会在左右留黑带（见 _align_scale 的说明）。
            if out["proposed"].get("crop"):
                tr["crop"] = out["proposed"]["crop"]
        new_foot["treatment"] = tr
        _write_json_atomic(foot_path, new_foot)

        out["applied"] = True
        out["next"].append("python %s --adopt --work %s   # 调色板变了 -> 设计指纹变了，重绑确认状态"
                           % (os.path.join(HERE, "propose.py"), work))
        out["next"].append("python %s --stage stills --work %s   # 亲眼看 3 张静帧再出片"
                           % (os.path.join(HERE, "render.py"), work))

    _write_json_atomic(work / "style_profile.json", out)
    if args.json:
        log(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        _print_report(out)
    return 0


def _print_report(p):
    m, f = p["measured"], p["family"]
    log("== matchstyle：素材 -> 自绘段 风格对齐")
    log("  素材: %d 帧（%s%s）色相 %.1f° %s 饱和 %.2f 明度 %.2f 亮度 %.2f"
        % (m["frames"], "EDL 内" if p["sampling"]["from"] == "edl" else "全片均匀",
           "，已含 treatment.color" if p["sampling"]["color_filter"] else "",
           m["hue"], p["hue_named"] or "", m["sat"], m["val"], m["luma"]))
    log("  底色: %s · h=%.1f s=%.3f v=%.3f（%s）"
        % ("暗色" if f["dark"] else "浅色", f["hue"], f["sat"], f["val"], f["note"]))
    pal = p["palette"]
    log("  调色板: bg=%s bg2=%s accent=%s ink=%s"
        % (pal["bg"], pal["bg2"], pal["accent"], pal["ink"]))
    lb = p["letterbox"]
    foot = lb["footage"]
    log("  画幅: 素材黑边 %d/%dpx（%s），自绘段 %s"
        % (foot.get("top", 0), foot.get("bottom", 0), foot.get("color") or "继承",
           ("%d/%dpx" % (lb["draw"]["top"], lb["draw"]["bottom"])) if lb["draw"] else "无遮幅"))
    for w in p["warnings"]:
        log("  [WARN] %s" % w)
    if p["applied"]:
        log("  已写入: design.visual.palette + footage.treatment%s"
            % ("（含 scale 对齐）" if p["proposed"].get("scale") else ""))
        log("  备份: %s / %s" % (p.get("backups", {}).get("design"),
                                 p.get("backups", {}).get("footage")))
    else:
        log("  只写了工作目录的 style_profile.json 画像快照，没改 design/footage。"
        "要落地：加 --apply（对齐画幅再加 --align-bars）")
    for n in p["next"]:
        log("  下一步: %s" % n)


# ============================================================================
# 7. CLI
# ============================================================================

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                    # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(
        prog="matchstyle.py",
        description="把素材的整体风格测出来，锚到 work/design.json 的调色板上（hybrid 专用）")
    ap.add_argument("--footage", default="footage.json",
                    help="素材线契约（默认 footage.json）")
    ap.add_argument("--work", default="work",
                    help="工作目录（读 edl.json，写 design.json 与 style_profile.json）")
    ap.add_argument("--design", default=None,
                    help="design.json 路径（默认 <work>/design.json）")
    ap.add_argument("--report", action="store_true",
                    help="只看画像，不改任何文件（不给 --apply 时就是它的行为）")
    ap.add_argument("--apply", action="store_true",
                    help="写 design.json 的调色板 + footage.json 的遮幅色")
    ap.add_argument("--align-bars", action="store_true",
                    help="连画幅一起对齐：改 treatment.scale，让素材黑边高度贴近自绘段")
    ap.add_argument("--sat-damp", type=float, default=0.6,
                    help="底色吃素材饱和度的比例（默认 0.6：太花调小，太闷调大）")
    ap.add_argument("--n-frames", type=int, default=9,
                    help="每个素材源最多抽几帧（默认 9；有 EDL 时按用到的镜头抽）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except RuntimeError as e:
        log("[matchstyle] %s" % e)
        return 1
    except KeyboardInterrupt:                                # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())


