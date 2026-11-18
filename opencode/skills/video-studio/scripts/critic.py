# -*- coding: utf-8 -*-
"""critic.py — 审美质检（离线启发式 + 可选 VLM 挂钩）。

为什么需要它
------------
这套技能的门禁全是"不能出错"：容器、信号、时间、页面、VMAF、ASR、剪影可区分。
没有一条回答"好不好看"。于是 agent 交出去的片子可能**一处硬伤都没有，但很丑**——
留白失衡、字号层级不分、配色低对比、元素贴边，这些都是"查得出来但没人查"的那一类。

两条路，默认离线：

  1) **离线启发式**（默认，零依赖除 numpy/PIL）：对候选联系表做可计算的美学体检——
     安全边距、亮度对比、色相分布、文字密度、空白率。输出 0-100 分与逐项扣分理由。
     它不懂"好看"，但它能抓住那四类最常见的翻车，而且**确定、可进 CI、不花钱**。
  2) **VLM 挂钩**（`--vlm-cmd`）：把联系表路径与结构化提问交给用户自己的命令（例如
     `claude -p`、本地 ollama、自建网关）。本脚本**不联网、不带 key、不猜 API**，
     只负责组 prompt、收 JSON、与离线分并排显示。

刻意不做的事
------------
* 不改 `proposal.json`（它有 schema，审美分不是提案契约的一部分）。
  结果单独落 `_critic.json` / `_critic.md`，与提案文件并存。
* 不参与 `--stage qa` 的账本：它评的是**候选**，不是成片；混进交付门禁会让
  "片子能交付"被一个主观分数绑架。

用法
----
    python critic.py --work work                      # 评 rev 最新一轮的全部候选
    python critic.py --work work --rev 2 --json       # 指定 rev，机读
    python critic.py --image a.png --image b.png      # 直接评若干张图
    python critic.py --work work --vlm-cmd "my-vlm"   # 附带 VLM 复评

退出码：0 = 评完（不论分数高低）；2 = 没有可评的图 / 参数错。**分数不设阈值**——
审美不该阻断交付，只该让人看见。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

try:
    import numpy as np
    from PIL import Image
except Exception as e:                                          # noqa: BLE001
    sys.stderr.write("critic 需要 numpy + Pillow：%s\n" % e)
    raise SystemExit(2) from None

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS = "proposals"

  # 打分口径：100 分制，逐项扣分。阈值来自"1080p 竖排海报"的经验值，
  # 不是学术标准——它是用来排序候选的，不是用来宣判美丑的。
W_MARGIN = 22       # 安全边距（最外侧内容离画框的距离）
W_CONTRAST = 24     # 明暗对比（正文与背景的可分离度）
W_HUE = 16          # 色相分散度（单调 = 廉价感）
W_DENSITY = 18      # 文字密度（太满/太空）
W_BALANCE = 20      # 视觉重心（左右上下是否失衡）


def _load(path):
    """读图 -> (rgb float32 [h,w,3] 0-1, 灰度 0-1)。RGB 之外的模式统一转 RGB。"""
    im = Image.open(path)
    im = im.convert("RGB")
    arr = np.asarray(im, dtype=np.float32) / 255.0
    gray = arr[..., 0] * 0.2126 + arr[..., 1] * 0.7152 + arr[..., 2] * 0.0722
    return arr, gray


def _content_box(gray, bg_tol=0.06):
    """粗估"有内容"的包围盒：与画面四周众数颜色差超过 bg_tol 的像素。

    众数用四边像素的中位数近似（比真众数快两个数量级，且对渐变底更是稳的）。
    """
    h, w = gray.shape
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    base = float(np.median(border))
    mask = np.abs(gray - base) > bg_tol
    # 去掉孤立噪点：按行/列命中率 > 1.5% 才算一行/一列有内容
    rows = np.where(mask.mean(axis=1) > 0.015)[0]
    cols = np.where(mask.mean(axis=0) > 0.015)[0]
    if rows.size == 0 or cols.size == 0:
        return None
    return int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])


def _contrast(gray):
    """明暗对比：**底色**与"最暗/最亮 0.3%"之间的可分离度。

    为什么不是简单的 p5-p95：文字只占画面 1% 上下时，任何"宽分位带"都落在
    底色自身的窄带里——实测一张正常浅底列表页 p5/p95 = 0.066（判成低对比、误报），
    p2/p98 = 0.081（仍误报），而 p1/p99 = 0.631、p0.5/p99.5 = 0.729。
    "字清不清楚"问的是**前景与底色差多少**，所以基准取底色（边框众数的中位数），
    另一端取尾部极值，并与两端的较大者取 max（深底浅字 / 浅底深字都成立）。
    """
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    base = float(np.median(border))
    lo = float(np.percentile(gray, 0.3))
    hi = float(np.percentile(gray, 99.7))
    return float(max(abs(base - lo), abs(hi - base)))


def _hue_spread(arr):
    """色相分散度：只统计"有饱和度"的像素，忽略大面积中性底色。"""
    mx = arr.max(axis=2)
    mn = arr.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    sel = sat > 0.18
    if sel.sum() < arr.shape[0] * arr.shape[1] * 0.005:
        return 0.0, 0.0
    r, g, b = arr[..., 0][sel], arr[..., 1][sel], arr[..., 2][sel]
    mx2, mn2 = mx[sel], mn[sel]
    d = np.maximum(mx2 - mn2, 1e-6)
    hue = np.where(
        mx2 == r, ((g - b) / d) % 6,
        np.where(mx2 == g, (b - r) / d + 2, (r - g) / d + 4)) * 60.0
    # 用 12 个 30° 桶的"有效桶数"衡量分散度：2 个桶 = 双色，6 个桶 = 丰富
    hist, _ = np.histogram(hue, bins=12, range=(0, 360))
    p = hist / max(1, hist.sum())
    nz = p[p > 0.02]
    eff = float(1.0 / np.sum(nz ** 2)) / 12.0    # 归一化到 0-1
    return eff, float(sel.mean())


def _text_density(gray, box):
    """文字密度代理：包围盒内高频边缘像素占比。太高=糊成一片，太低=空得发慌。"""
    if not box:
        return 0.0
    y0, y1, x0, x1 = box
    crop = gray[y0:y1 + 1, x0:x1 + 1]
    if crop.size < 64:
        return 0.0
    gx = np.abs(np.diff(crop, axis=1))
    gy = np.abs(np.diff(crop, axis=0))
    edges = ((gx > 0.12).mean() + (gy > 0.12).mean()) / 2.0
    return float(edges)


def _balance(box, shape):
    """视觉重心偏移：内容包围盒中心与画面中心在 x 方向的偏差（占宽比）。"""
    if not box:
        return 0.0
    y0, y1, x0, x1 = box
    h, w = shape
    cx = (x0 + x1) / 2.0 / max(1, w - 1)
    return float(abs(cx - 0.5) * 2.0)


def _looks_like_single_frame(h, w):
    """宽高比接近画布 = 单帧；否则当作联系表/拼图，跳过安全区判定。

    用 1.6:1 到 2.2:1 的宽区间而不是精确 16:9：候选联系表也可能是 2 帧或 4 帧拼成，
    只要明显更"宽"就说明它不是一个 16:9 的画框。
    """
    if h <= 0:
        return False
    ar = w / float(h)
    return 1.6 <= ar <= 2.2


def _safe_rect(h, w):
    """取当前画布的安全区；layoutkit 不可用时按 16:9 比例折算（不硬编码 1080p 坐标）。

    折算而不是照抄常量：critic 可能被单独拷去评一张 720p 的图，写死 (64,58,1856,974)
    会在 720p 上把整幅内容判成越界。
    """
    try:
        sys.path.insert(0, HERE)
        import layoutkit
        if (layoutkit.W, layoutkit.H) == (w, h):
            return layoutkit.SAFE
        k = min(w / float(layoutkit.W), h / float(layoutkit.H))
        sx0, sy0, sx1, sy1 = layoutkit.SAFE
        return (sx0 * k, sy0 * k, sx1 * k, sy1 * k)
    except Exception:                                          # noqa: BLE001
        return (0.033 * w, 0.054 * h, 0.967 * w, 0.902 * h)


def score_image(path):
    """单图打分。返回 {path, score, metrics, notes}。"""
    arr, gray = _load(path)
    h, w = gray.shape
    box = _content_box(gray)
    notes = []
    score = 100.0

    # 1) 安全区：判据用**设计契约**（layoutkit.SAFE）而不是"离画框多远"。
    #
    # 第一版用的是"内容离画框 < 2.5% = 贴边"，实测把**正常的全出血底色 + 左侧贴边色带**
    # 判成"边距偏窄 4.8%"——因为盒子里装的是装饰色带，不是文字。而这条技能的契约
    # 本来写得清清楚楚：正文 y∈SAFE，字幕 y∈SUB_BAND。所以按 SAFE 判越界才是有意义的口径。
    #
    # 越界 <= 1% 短边算容差（描边/渐晕会溢出几像素），超过才扣分。
    #
    # **联系表不算安全区**：`_sheet.png` 是 N 帧并排的拼图（实测 1440x270），它的
    # 边界由拼贴决定，与"正文离画框多远"无关——按 SAFE 判会稳定误报 700+px。
    # 所以只在"单帧"（宽高比接近画布）上判边距，拼图上跳过这一项、并在输出里说明。
    single = _looks_like_single_frame(h, w)
    if box and single:
        sx0, sy0, sx1, sy1 = _safe_rect(h, w)
        tol = 0.01 * min(h, w)
        over = max(sx0 - box[2], box[3] - sx1, sy0 - box[0], box[1] - sy1, 0.0)
        m = min(box[0], h - 1 - box[1], box[2], w - 1 - box[3]) / float(min(h, w))
        if over > tol * 3:
            score -= W_MARGIN
            notes.append("内容压出安全区（越界 %.0fpx）" % over)
        elif over > tol:
            score -= W_MARGIN * 0.5
            notes.append("内容轻微越出安全区（%.0fpx）" % over)
        elif m < 0.02:
            # 只有**贴到画框边**（<2% 短边）才算问题，且降权：装饰色带本就该出血。
            score -= W_MARGIN * 0.3
            notes.append("内容贴到画框边（%.1f%%）" % (m * 100))
    elif not single:
        notes.append("联系表（多帧拼图）：跳过安全区判定")
    else:
        score -= W_MARGIN * 0.5
        notes.append("几乎无内容（整格接近纯色）")

    # 2) 明暗对比
    c = _contrast(gray)
    if c < 0.18:
        score -= W_CONTRAST
        notes.append("明暗对比过低（%.2f）" % c)
    elif c < 0.30:
        score -= W_CONTRAST * 0.45
        notes.append("明暗对比偏低（%.2f）" % c)

    # 3) 色相分散度
    eff, sat_frac = _hue_spread(arr)
    if sat_frac > 0.01 and eff < 0.12:
        score -= W_HUE * 0.6
        notes.append("配色单调（有效色相桶 %.2f）" % eff)

    # 4) 文字密度
    td = _text_density(gray, box)
    if td > 0.16:
        score -= W_DENSITY
        notes.append("文字/边缘过密（%.2f）" % td)
    elif td < 0.012:
        score -= W_DENSITY * 0.5
        notes.append("画面偏空（%.3f）" % td)

    # 5) 重心平衡
    bal = _balance(box, (h, w))
    if bal > 0.30:
        score -= W_BALANCE
        notes.append("重心偏一侧（偏移 %.0f%%）" % (bal * 100))

    return {
        "path": path,
        "score": round(max(0.0, score), 1),
        "metrics": {
            "margin": round(min(box[0], h - 1 - box[1], box[2], w - 1 - box[3]) / float(min(h, w)), 4) if box else 0.0,
            "contrast": round(c, 4),
            "hue_eff": round(eff, 4),
            "text_density": round(td, 4),
            "balance": round(bal, 4),
            "size": [w, h],
        },
        "notes": notes,
    }


def _label_of(path):
    """给图起一个人读的短名。

    `propose` 的候选联系表统一叫 `_sheet.png`（在各自的候选目录里），只取 basename
    会得到三个一模一样的 "_sheet"——分不清哪个是 A 哪个是 B。所以下划线开头的名字
    改用**父目录名**（候选 id），这才是人脑里"候选 A"的那个 A。
    """
    base = os.path.splitext(os.path.basename(path))[0]
    if base.startswith("_"):
        parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
        if parent:
            return parent
    return base


def _latest_rev(work):
    """找 proposals/ 下最大 rev 编号（rev1/rev2/...）。"""
    base = os.path.join(work, PROPOSALS)
    if not os.path.isdir(base):
        return None
    revs = [d for d in os.listdir(base)
            if d.startswith("rev") and d[3:].isdigit() and os.path.isdir(os.path.join(base, d))]
    return max(revs, key=lambda d: int(d[3:])) if revs else None


def _sheets(work, rev):
    """候选联系表：proposals/<rev>/<ID>/_sheet.png；没有就去评并排图。"""
    d = os.path.join(work, PROPOSALS, rev)
    found = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            sub = os.path.join(d, name)
            if os.path.isdir(sub):
                p = os.path.join(sub, "_sheet.png")
                if os.path.isfile(p):
                    found.append((name, p))
    cmp_path = os.path.join(d, "_compare.png")
    if not found and os.path.isfile(cmp_path):
        found.append(("_compare", cmp_path))
    return found




def _vlm_prompt(paths):
    """给 VLM 的结构化提问。要求**只输出 JSON**，便于机器合并。"""
    return (
        "你是视频版式评审。下面是同一部片子的候选静帧联系表（每个候选一行）。\n"
        "逐张回答四件事，只输出 JSON 数组，不要解释：\n"
        "  [{cite: 文件名, margin: 0-10, hierarchy: 0-10, contrast: 0-10, "
        "   crowding: 0-10, worst: 一处最该改的具体问题}]\n"
        "评分口径：10 = 专业级，5 = 能用但普通，0 = 明显翻车。\n"
        "图片：\n" + "\n".join(paths)
    )


def _run_vlm(cmd, paths):
    """把 prompt 交给用户给的命令。stdin 传 prompt，stdout 收 JSON。不联网、不猜 key。"""
    prompt = _vlm_prompt(paths)
    argv = shlex.split(cmd)
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=600)
    except Exception as e:                                        # noqa: BLE001
        return {"ok": False, "error": "VLM 命令执行失败：%s" % e}
    if p.returncode != 0:
        return {"ok": False, "error": "VLM 命令退出码 %d：%s"
                % (p.returncode, (p.stderr or "")[:300])}
    raw = (p.stdout or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return {"ok": False, "error": "VLM 输出里没有 JSON 数组：%s" % raw[:200]}
    try:
        return {"ok": True, "items": json.loads(raw[start:end + 1])}
    except Exception as e:                                        # noqa: BLE001
        return {"ok": False, "error": "VLM JSON 解析失败：%s" % e}


def _setup_stdio():
    """Windows 管道下默认 gbk，会把中文打印成乱码——其它脚本都这么兜过。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="审美质检：离线启发式打分（默认）+ 可选 VLM 复评")
    ap.add_argument("--work", default="work", help="工程目录（找 proposals/rev*/<ID>/_sheet.png）")
    ap.add_argument("--rev", default="", help="指定 rev（默认最新一轮）")
    ap.add_argument("--image", action="append", default=[], help="直接评这张图（可重复）")
    ap.add_argument("--vlm-cmd", default="", help="VLM 命令：prompt 走 stdin，stdout 收 JSON")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--out", default="", help="额外落盘到该文件（.json）")
    a = ap.parse_args(argv)
    _setup_stdio()

    targets = []
    if a.image:
        targets = [(_label_of(p), p) for p in a.image]
    else:
        rev = a.rev or _latest_rev(a.work)
        if not rev:
            sys.stderr.write("没有找到 proposals/rev*/ —— 先跑 propose.py\n")
            return 2
        targets = _sheets(a.work, rev)
        if not targets:
            sys.stderr.write("%s 下没有 _sheet.png（提案可能没出联系表）\n" % rev)
            return 2

    rows = []
    for name, path in targets:
        try:
            r = score_image(path)
        except Exception as e:                                    # noqa: BLE001
            rows.append({"candidate": name, "path": path, "error": str(e)})
            continue
        r["candidate"] = name
        rows.append(r)

    payload = {"schema": 1, "engine": "offline-heuristic", "items": rows}
    if a.vlm_cmd:
        payload["vlm"] = _run_vlm(a.vlm_cmd, [p for _, p in targets])

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("审美质检（离线启发式，0-100，只用于排序不用于判定）")
    print("-" * 66)
    for r in rows:
        if "error" in r:
            print("  %-4s 读取失败：%s" % (r.get("candidate"), r["error"]))
            continue
        print("  %-4s %5.1f  %s" % (r["candidate"], r["score"], "; ".join(r["notes"]) or "无明显问题"))
    if a.vlm_cmd:
        v = payload.get("vlm") or {}
        print("-" * 66)
        if v.get("ok"):
            for it in v.get("items", []):
                print("  VLM %s：层次 %s / 边距 %s / 对比 %s / 拥挤 %s → %s"
                      % (it.get("cite"), it.get("hierarchy"), it.get("margin"),
                         it.get("contrast"), it.get("crowding"), it.get("worst")))
        else:
            print("  VLM 未拿到结果：%s" % v.get("error"))
    print("-" * 66)
    print("提示：分数低不等于不能交付，但**同一批候选里分差 > 15 分**通常说明该换一个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())