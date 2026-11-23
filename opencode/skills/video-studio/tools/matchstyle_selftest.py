# -*- coding: utf-8 -*-
"""matchstyle_selftest.py — hybrid 风格对齐（matchstyle.py）的离线自检。

跑法：python tools/matchstyle_selftest.py [-v]

为什么单独一个自检文件而不是塞进 footage_selftest：那个文件验的是"素材怎么剪"，
这个验的是"两条产线怎么长得像一个片子"。同一台机器上一个缺 ffmpeg 就整份跳过，
混在一起会让"颜色对不对"和"剪得对不对"的结论互相掩盖。

合成源是一块**已知色相**的画面（RGB 32/48/64 = HSV 210°），所以断言可以写成
"测出来的色相必须落在 210° 附近"，而不是"看起来差不多"。

覆盖的行为（每条都对应一个会真实发生的坑）：
  1. 抽样统计能测出素材的主色相与明暗（不是拿平均值冒充）
  2. --report 一个文件都不改（错在"以为 --report 也会写盘"，改前发现不了）
  3. --apply 写进去的调色板同族、且过 design_to_plan 的 theme_lint
  4. 素材段没被上色：treatment.color 不被本脚本写入，素材保持原色
  5. 缺 design.json 时 --apply 失败退出且**零写入**（fail-closed）
  6. --align-bars 把素材可见高算到自绘段的画幅
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
_RESULTS = []

# 合成源的主色：HSV(210, 0.50, 0.25) -> 深蓝灰（暗素材）
SRC_RGB = (32, 48, 64)
SRC_HUE = 210.0


def check(name, fn):
    try:
        detail = fn()
        _RESULTS.append(("PASS", name, detail or ""))
    except Exception as e:                                  # noqa: BLE001
        _RESULTS.append(("FAIL", name, "%s: %s" % (type(e).__name__, e)))


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg（matchstyle 靠它抽帧）")
    return exe


def build_source(tmp):
    """合成一段 6 秒、色相固定的素材（噪声只是为了让编码不像纯色块）。"""
    import numpy as np
    d = Path(tmp) / "media"
    d.mkdir(parents=True, exist_ok=True)
    out = d / "source.mp4"
    w, h, fps, secs = 640, 360, 12, 6
    rng = np.random.RandomState(11)
    frames = []
    for _ in range(fps * secs):
        noise = rng.randint(-6, 7, size=(h, w, 1)).astype(np.int16)
        base = np.array(SRC_RGB, dtype=np.int16)[None, None, :] + noise
        frames.append(np.clip(base, 0, 255).astype(np.uint8))
    raw = b"".join(f.tobytes() for f in frames)
    cmd = [ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (w, h), "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(out)]
    p = subprocess.run(cmd, input=raw, capture_output=True)
    assert p.returncode == 0, "合成素材失败：%s" % p.stderr.decode("utf-8", "replace")[-300:]
    return out


def write_footage(tmp, src):
    foot = {
        "title": "matchstyle 自检素材",
        "sources": [{"id": "s1", "kind": "video", "path": str(src),
                     "license_note": "自检合成画面"}],
        "treatment": {"letterbox_color": "0x0b0c0e"},
        "video": {"width": 1920, "height": 1080, "fps": 30},
        "audio": {"voice": "zh-CN-YunyangNeural"},
        "lines": [{"id": "l1", "text": "一句话", "sub": "一句话",
                   "shot": {"source": "s1", "at": 1.0, "why": "自检"}}],
    }
    p = Path(tmp) / "footage.json"
    p.write_text(json.dumps(foot, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def write_design(work, motif=None):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    d = json.loads((ROOT / "assets" / "design_fixture.json").read_text(encoding="utf-8"))
    if motif:
        d["visual"]["motif"] = {"kind": motif, "density": 0.3,
                                "detail": {"top": 40, "bottom": 44}}
    p = work / "design.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def run(*args):
    cmd = [sys.executable, str(SCRIPTS / "matchstyle.py")] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def _hue_gap(a, b):
    d = abs((float(a) - float(b)) % 360.0)
    return d if d <= 180.0 else 360.0 - d


def _profile(work):
    p = Path(work) / "style_profile.json"
    assert p.is_file(), "没有写出 style_profile.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 用例

def t_profile(env):
    tmp, src = env["tmp"], env["src"]
    work = Path(tmp) / "w_profile"
    foot = write_footage(tmp, src)
    r = run("--footage", str(foot), "--work", str(work), "--report", "--json")
    assert r.returncode == 0, "report 应成功：%s" % (r.stdout + r.stderr)[-300:]
    p = _profile(work)
    m = p["measured"]
    assert _hue_gap(m["hue"], SRC_HUE) <= 12.0, \
        "主色相测偏了：%.1f（应≈%.0f）" % (m["hue"], SRC_HUE)
    assert m["luma"] < 0.35, "深色素材应判成暗底，实测 luma=%.3f" % m["luma"]
    assert p["family"]["dark"] is True, "family.dark 与 luma 判据不一致"
    assert not p["applied"], "只跑 --report 却标了 applied"
    assert len(p["palette"]) == 11, "调色板应有 11 个色"
    return "hue=%.1f luma=%.3f dark=%s" % (m["hue"], m["luma"], p["family"]["dark"])


def t_report_untouched(env):
    tmp, src = env["tmp"], env["src"]
    work = Path(tmp) / "w_ro"
    foot = write_footage(tmp, src)
    design = write_design(work)
    before = (design.read_bytes(), foot.read_bytes())
    r = run("--footage", str(foot), "--work", str(work), "--report")
    assert r.returncode == 0, r.stdout + r.stderr
    after = (design.read_bytes(), foot.read_bytes())
    assert before == after, "--report 改了文件（只该写 style_profile.json）"
    return "design/footage 未变"


def t_apply(env):
    tmp, src = env["tmp"], env["src"]
    work = Path(tmp) / "w_apply"
    foot = write_footage(tmp, src)
    design = write_design(work)
    r = run("--footage", str(foot), "--work", str(work), "--apply")
    assert r.returncode == 0, "apply 应成功：%s" % (r.stdout + r.stderr)[-400:]
    p = _profile(work)
    assert p["applied"] is True, "apply 没标记 applied"
    got = json.loads(design.read_text(encoding="utf-8"))
    pal = got["visual"]["palette"]
    # 同族：底色色相必须贴近素材，否则"对齐"是假的
    import stylekit
    hue = stylekit.rgb_to_hsv(tuple(pal["bg"]))[0]
    assert _hue_gap(hue, SRC_HUE) <= 15.0, \
        "写进去的底色色相 %.0f° 与素材 %.0f° 不同族" % (hue, SRC_HUE)
    # 必须过渲染侧的校验（含 theme_lint）
    import designkit
    designkit.design_to_plan(got)
    # 素材段不被上色：treatment 里不许出现 color
    newf = json.loads(foot.read_text(encoding="utf-8"))
    assert not (newf.get("treatment") or {}).get("color"), \
        "matchstyle 往素材 treatment 里塞了 color（会把素材洗色）"
    lb = str(newf["treatment"]["letterbox_color"])
    assert lb.startswith("0x") and len(lb) == 8, "letterbox_color 写法不对：%r" % lb
    rgb = tuple(int(lb[i:i + 2], 16) for i in (2, 4, 6))
    assert stylekit.luminance(rgb) < 0.05, "遮幅色不够黑（%.3f），拼接处会跳" % stylekit.luminance(rgb)
    assert not (work / "backup").exists() or (work / "backup").is_dir(), "备份目录异常"
    return "bg_hue=%.0f letterbox=%s" % (hue, lb)


def t_failclosed(env):
    tmp, src = env["tmp"], env["src"]
    work = Path(tmp) / "w_fail"
    work.mkdir(parents=True, exist_ok=True)
    foot = write_footage(tmp, src)
    before = foot.read_bytes()
    r = run("--footage", str(foot), "--work", str(work), "--apply")
    assert r.returncode == 2, "缺 design.json 时 --apply 应退 2，实际 %d" % r.returncode
    assert foot.read_bytes() == before, "失败路径仍然改了 footage.json"
    assert not (work / "design.json").exists(), "失败路径凭空造了 design.json"
    return "exit=2 零写入"


def t_align_bars(env):
    tmp, src = env["tmp"], env["src"]
    work = Path(tmp) / "w_bars"
    foot = write_footage(tmp, src)
    write_design(work, motif="letterbox")          # 只为了有一份带遮幅的设计可用
    r = run("--footage", str(foot), "--work", str(work), "--apply", "--align-bars")
    assert r.returncode == 0, r.stdout + r.stderr
    p = _profile(work)
    scale = p["proposed"].get("scale") or ""
    assert ":" in scale, "--align-bars 没算出 scale：%r" % scale
    sh = int(scale.split(":")[1])
    assert abs(sh - (1080 - 40 - 44)) <= 6, \
        "可见高 %d 没贴到自绘段画幅 %d" % (sh, 1080 - 40 - 44)
    newf = json.loads(foot.read_text(encoding="utf-8"))
    assert newf["treatment"].get("scale") == scale, "scale 没写进 footage.json"
    # 只改 scale 会在左右留黑带（1600x900 素材对齐 996 高时右侧黑带 150px）：
    # 取景窗必须跟着收缩，宽高比刚好等于目标画幅。这条断言就是那个 bug 的回归。
    crop = newf["treatment"].get("crop") or ""
    assert crop, "--align-bars 没写 crop：左右会出现黑带"
    cw, ch = (int(x) for x in crop.split(":")[:2])
    assert abs(cw / float(ch) - 1920.0 / (1080 - 40 - 44)) < 0.01, \
        "取景窗宽高比 %d:%d 与目标画幅不符（左右仍会冒黑带）" % (cw, ch)
    bars = p["letterbox"]
    assert bars["draw"] is not None, "自绘段遮幅没被识别出来"
    return "scale=%s crop=%s 目标可见高=%d" % (scale, crop, 1080 - 40 - 44)


def main():
    tmp = tempfile.mkdtemp(prefix="matchstyle-")
    try:
        src = build_source(tmp)
        env = {"tmp": tmp, "src": src}
        check("1 抽样统计能测出素材主色相/明暗", lambda: t_profile(env))
        check("2 --report 零写入", lambda: t_report_untouched(env))
        check("3 --apply 调色板同族且过 theme_lint", lambda: t_apply(env))
        check("4 缺 design.json 时 fail-closed", lambda: t_failclosed(env))
        check("5 --align-bars 对齐画幅", lambda: t_align_bars(env))
    finally:
        if not VERBOSE:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("保留临时目录：%s" % tmp)

    bad = [r for r in _RESULTS if r[0] != "PASS"]
    for status, name, detail in _RESULTS:
        print("[%s] %s%s" % (status, name, ("  —  " + detail) if (detail and status != "PASS") else ""))
    print("-" * 66)
    print("%d/%d 通过" % (len(_RESULTS) - len(bad), len(_RESULTS)))
    if bad:
        for _s, name, detail in bad:
            print("  FAIL %s: %s" % (name, detail))
        return 1
    return 0


def _setup_stdio():
    """重定向到管道/文件时切 UTF-8：Windows 默认代码页是 GBK，本文件输出里有
    em dash（—）与中文，Agent 用 subprocess 抓输出时既会乱码，也可能
    UnicodeEncodeError 直接崩。与 selfcheck.py / render.py 同一口径。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


if __name__ == "__main__":
    _setup_stdio()
    sys.exit(main())
