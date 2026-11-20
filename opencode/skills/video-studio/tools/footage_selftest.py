# -*- coding: utf-8 -*-
"""footage_selftest.py — 素材剪辑生产线（footage 模式）的离线自检。

跑法：python tools/footage_selftest.py [-v]

它自己造素材（PIL 画一张带"烧录字幕 + 右上水印"的合成画面），然后跑完整链路：

    probe -> tts -> edl -> cut -> mix -> assemble -> qa

断言的是**这条线最贵的几件事**（都是真实出片时踩过的）：

  1. EDL 越界能被 F1 拦住（at+len 超过源时长）
  2. 烧录字幕没裁干净 -> F2 fail；裁干净 -> F2 pass（几何判定，不看画面像不像字）
  3. 水印没裁干净 -> F3 fail；裁干净 -> F3 pass
  4. 音画等长（F4）与片尾不被 -shortest 吃掉
  5. 出处卡/授权说明缺了 -> F5 fail
  6. 联系表生成（F7 的证据）与签核指纹随内容变化而失效

不需要网络：旁白用合成 wav 顶替 TTS（filmkit.tts 的缓存契约照旧）。
需要 ffmpeg + numpy + PIL；缺 ffmpeg 直接 FAIL（这条线没有 ffmpeg 什么都做不了）。
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


def check(name, fn):
    try:
        detail = fn()
        _RESULTS.append(("PASS", name, detail or ""))
    except Exception as e:                                  # noqa: BLE001
        _RESULTS.append(("FAIL", name, "%s: %s" % (type(e).__name__, e)))


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg（素材线必须要有）")
    return exe


def need_mods():
    import filmkit, footagekit, footageedit, footageqa  # noqa: F401
    return filmkit, footagekit, footageedit, footageqa


def build_source(tmp):
    """合成测试源：均匀噪声底 + 移动色块 + 静态烧录字幕 + 静态右上水印。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    W, H = 960, 412                       # 用 960x412 省时间（比例同 1920x824）
    d = Path(tmp) / "media"
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(7)
    base = (rng.rand(H, W, 1) * 46 + 34).astype(np.uint8)
    base = np.repeat(base, 3, axis=2)
    Image.fromarray(base).save(d / "bg.png")

    font = None
    for cand in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/simhei.ttf",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"):
        if Path(cand).exists():
            font = cand
            break
    sub = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(sub)
    f = ImageFont.truetype(font, 22) if font else ImageFont.load_default()
    text = "就是因为你这个办法我们才走到了今天"
    tw = dr.textlength(text, font=f) if font else 200
    dr.rectangle([0, 350, W, 400], fill=(0, 0, 0, 190))
    dr.text(((W - tw) / 2, 364), text, font=f, fill=(255, 255, 255, 255))
    sub.save(d / "subs.png")

    wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dw = ImageDraw.Draw(wm)
    f2 = ImageFont.truetype(font, 14) if font else ImageFont.load_default()
    dw.rectangle([W - 180, 8, W - 10, 32], fill=(255, 255, 255, 45))
    dw.text((W - 172, 12), "经典珍藏 bilibili", font=f2, fill=(255, 255, 255, 215))
    wm.save(d / "wm.png")

    out = d / "source.mp4"
    subprocess.run([
        ffmpeg(), "-v", "error", "-y",
        "-loop", "1", "-t", "24", "-i", str(d / "bg.png"),
        "-f", "lavfi", "-t", "24", "-i", "color=c=0xB8C6D9@0.85:size=180x140:rate=30",
        "-loop", "1", "-t", "24", "-i", str(d / "subs.png"),
        "-loop", "1", "-t", "24", "-i", str(d / "wm.png"),
        "-filter_complex",
        "[0:v][1:v]overlay=x='mod(t*180,760)-120':y='120+60*sin(t/3)':shortest=1[a];"
        "[a][2:v]overlay=0:0[b];[b][3:v]overlay=0:0,fps=30,format=yuv420p[v]",
        "-map", "[v]", "-t", "24", "-c:v", "libx264", "-crf", "22", "-preset", "veryfast",
        str(out),
    ], check=True)
    return str(out), W, H


def build_voice(work, lines):
    """合成旁白：不用网络，直接生成 wav，并把 meta 写成 filmkit.tts 的缓存格式。"""
    import numpy as np
    import filmkit
    tts_dir = Path(work) / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, ln in enumerate(lines):
        dur = 2.2 + 0.35 * i
        n = int(dur * filmkit.SR)
        t = np.arange(n) / float(filmkit.SR)
        tone = (0.24 * np.sin(2 * np.pi * (320 + 40 * i) * t)).astype(np.float32)
        fade = np.ones(n, dtype=np.float32)
        k = min(1600, n // 4)
        fade[:k] = np.linspace(0, 1, k)
        fade[-k:] = np.linspace(1, 0, k)
        pcm = (tone * fade)[:, None] * np.ones((1, 2), dtype=np.float32)
        mp3 = tts_dir / (ln["id"] + ".mp3")
        filmkit.wav_write(str(mp3), pcm)          # 后缀写成 .mp3 也无妨：解码走 ffmpeg
        rows.append(dict(ln, path=str(mp3), mp3=str(mp3), dur=round(dur, 3),
                         key="selftest", spec_version=2, trimmed=False))
    (Path(work) / "tts.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def make_footage(src, W, H, sub_band, wm_box, crop, with_credit=True, with_lic=True):
    lines = [
        {"id": "s01", "text": "第一句用于自检的旁白。", "sub": "第一句",
         "shot": {"source": "src1", "at": 2.0}},
        {"id": "s02", "text": "第二句更长的旁白，用来验证时间轴跟随实测时长。",
         "shot": {"source": "src1", "at": 8.0}},
    ]
    src_rec = {"id": "src1", "kind": "video", "path": src,
               "burned_subs": {"y0": sub_band[0], "y1": sub_band[1]},
               "watermark": {"x": wm_box[0], "y": wm_box[1], "w": wm_box[2], "h": wm_box[3]}}
    if with_lic:
        src_rec["license_note"] = "合成测试素材（自检用），非真实影视内容"
    foot = {"title": "footage 自检片", "sources": [src_rec],
            "treatment": {"crop": crop, "scale": "960:404", "letterbox_color": "0x0b0c0e"},
            "video": {"width": 1920, "height": 1080, "fps": 30},
            "audio": {"env_gain": 0.12, "music_gain": 0.08, "words": False,
                      "music": {"bpm": 92, "key": "Dm", "scale": "minor",
                                "mood": "steady", "seed": 3}},
            "subtitle": {"size": 40, "margin_v": 30},
            "pacing": {"lead": 0.25, "tail": 0.4, "outro": 1.2},
            "lines": lines}
    if with_credit:
        foot["credit"] = {"text": "素材：自检合成源"}
    return foot


def run_chain(foot, work, out, edl_strict=False):
    """直接调函数跑完整链路（等价于 footage.py --stage all）。"""
    filmkit, footagekit, footageedit, footageqa = need_mods()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "sources.json").write_text(
        json.dumps({s["id"]: footagekit.probe_file(s["path"]) for s in foot["sources"]},
                   ensure_ascii=False), encoding="utf-8")
    rows = build_voice(work, foot["lines"])
    tl = footageedit.build_timeline(foot, rows)
    info = {s["id"]: footagekit.probe_file(s["path"]) for s in foot["sources"]}
    edl, problems = footageedit.make_edl(foot, tl, sources_info=info)
    if problems and edl_strict:
        raise RuntimeError("EDL 有问题：" + "；".join(problems))
    cut = footageedit.cut_shots(foot, edl, work)
    mix = footageedit.mix_tracks(rows, tl, cut, foot, work)
    ass = footageedit.write_ass(tl, foot, work / "sub.ass")
    silent = footageedit.concat_video(cut, work)
    if not Path(silent).is_file():          # 无声成片是 assemble 的输入，产物缺了要在这里说清楚
        raise RuntimeError("concat 没有产出无声成片：%s" % silent)
    movie = footageedit.assemble(foot, work, out, ass_path=ass["path"])
    rep = footageqa.run_all(foot, work, movie, tl, edl, strict=False)
    return dict(timeline=tl, edl=edl, cut=cut, mix=mix, movie=movie, report=rep)


def gates(report):
    return {g["gate"]: g for g in report["gates"]}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                        # noqa: BLE001
        pass
    print("footage 自检（离线；需要 ffmpeg + numpy + PIL）")
    print("-" * 66)
    tmp = tempfile.mkdtemp(prefix="footage_selftest_")
    try:
        src, W, H = build_source(tmp)
        sub_band = (int(H * 0.85), H)          # 烧录字幕在底部 ~15%
        wm_box = (W - 190, 6, 182, 30)
        # 治理配方：裁掉底部字幕带 + 顶部水印带，保留中间
        # 可见区必须同时避开：底部字幕带（y>=0.85H）与右上水印（x>=W-190, y<=36）
        safe_crop = "%d:%d:%d:%d" % (W - 220, int(H * 0.70), 10, 44)
        bad_crop = "%d:%d:0:0" % (W, H)

        def t_clean():
            foot = make_footage(src, W, H, sub_band, wm_box, safe_crop)
            r = run_chain(foot, Path(tmp) / "w_clean", Path(tmp) / "clean.mp4")
            g = gates(r["report"])
            assert g["F1"]["status"] == "pass", "F1: " + g["F1"]["detail"]
            assert g["F2"]["status"] == "pass", "F2 应 pass: " + g["F2"]["detail"]
            assert g["F3"]["status"] == "pass", "F3 应 pass: " + g["F3"]["detail"]
            assert g["F4"]["status"] == "pass", "F4: " + g["F4"]["detail"]
            assert g["F5"]["status"] == "pass", "F5: " + g["F5"]["detail"]
            return "F2/F3 判 pass（crop 盖住字幕带与水印带）"

        def t_dirty():
            foot = make_footage(src, W, H, sub_band, wm_box, bad_crop)
            r = run_chain(foot, Path(tmp) / "w_dirty", Path(tmp) / "dirty.mp4")
            g = gates(r["report"])
            assert g["F2"]["status"] == "fail", "F2 应 fail（字幕没裁）: " + g["F2"]["detail"]
            assert g["F3"]["status"] == "fail", "F3 应 fail（水印没裁）: " + g["F3"]["detail"]
            return "F2/F3 都能抓到未治理的素材"

        def t_partial():
            """只裁顶部（去掉水印）但不动底部字幕：F3 pass、F2 fail —— 两条门禁互不干扰。"""
            # 只裁顶部到水印之下（y=44 起），底部字幕保持可见
            crop = "%d:%d:0:%d" % (W, int(H * 0.92), 44)
            foot = make_footage(src, W, H, sub_band, wm_box, crop)
            r = run_chain(foot, Path(tmp) / "w_partial", Path(tmp) / "partial.mp4")
            g = gates(r["report"])
            assert g["F3"]["status"] == "pass", "F3 应 pass: " + g["F3"]["detail"]
            assert g["F2"]["status"] == "fail", "F2 应 fail: " + g["F2"]["detail"]
            return "只去水印时 F3 pass / F2 fail（两条判据独立）"

        def t_no_license():
            foot = make_footage(src, W, H, sub_band, wm_box, safe_crop,
                                with_credit=False, with_lic=False)
            r = run_chain(foot, Path(tmp) / "w_nolic", Path(tmp) / "nolic.mp4")
            g = gates(r["report"])
            assert g["F5"]["status"] == "fail", "F5 应 fail（缺 license_note / 出处卡）"
            return "F5 抓到缺出处"

        def t_out_of_range():
            foot = make_footage(src, W, H, sub_band, wm_box, safe_crop)
            foot["lines"][1]["shot"]["at"] = 22.0          # 22 + 4s > 24s 源长
            _, footagekit, footageedit, _qa = need_mods()
            work = Path(tmp) / "w_oor"
            work.mkdir(parents=True, exist_ok=True)
            rows = build_voice(work, foot["lines"])
            tl = footageedit.build_timeline(foot, rows)
            info = {s["id"]: footagekit.probe_file(s["path"]) for s in foot["sources"]}
            _edl, problems = footageedit.make_edl(foot, tl, sources_info=info)
            assert problems, "越界镜头没有被 check_edl 拦住"
            assert any("越界" in p for p in problems), "问题描述里没有'越界'：%s" % problems
            return "F1/check_edl 拦住 at+len 越界"

        def t_contact_sheet():
            foot = make_footage(src, W, H, sub_band, wm_box, safe_crop)
            r = run_chain(foot, Path(tmp) / "w_sheet", Path(tmp) / "sheet.mp4")
            sheet = r["report"]["contact_sheet"]
            assert sheet and Path(sheet).exists(), "联系表没生成"
            fp1 = r["report"]["fingerprint"]
            foot2 = make_footage(src, W, H, sub_band, wm_box, safe_crop)
            foot2["lines"][0]["text"] = "改过的台词，指纹应该变。"
            _, _fk, footageedit, footageqa = need_mods()
            work2 = Path(tmp) / "w_sheet2"
            work2.mkdir(parents=True, exist_ok=True)
            rows = build_voice(work2, foot2["lines"])
            tl2 = footageedit.build_timeline(foot2, rows)
            edl2, _p = footageedit.make_edl(foot2, tl2)
            fp2 = footageqa._fingerprint(foot2, edl2)
            assert fp1 != fp2, "改了台词但签核指纹没变"
            return "联系表生成 + 指纹随内容变化"

        def t_av_equal():
            foot = make_footage(src, W, H, sub_band, wm_box, safe_crop)
            r = run_chain(foot, Path(tmp) / "w_av", Path(tmp) / "av.mp4")
            filmkit, _fk, _fe, _qa = need_mods()
            d = filmkit.probe(r["movie"]) or {}
            dur = float((d.get("format") or {}).get("duration") or 0)
            want = float(r["timeline"]["total"])
            assert abs(dur - want) < 0.2, "成片 %.2fs != 时间轴 %.2fs" % (dur, want)
            return "成片时长与时间轴一致（%.2fs）" % dur

        for name, fn in (("治理干净的素材 F2/F3 通过", t_clean),
                         ("未治理的素材 F2/F3 都能抓到", t_dirty),
                         ("只去水印时 F2/F3 判据独立", t_partial),
                         ("缺出处被 F5 拦住", t_no_license),
                         ("越界镜头被 check_edl 拦住", t_out_of_range),
                         ("联系表 + 签核指纹", t_contact_sheet),
                         ("音画等长", t_av_equal)):
            check(name, fn)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for s, _, _ in _RESULTS if s == "PASS")
    n_fail = len(_RESULTS) - n_pass
    for st, name, detail in _RESULTS:
        if st == "PASS":
            print("  PASS %-28s %s" % (name, detail if VERBOSE else ""))
        else:
            print("  FAIL %-28s %s" % (name, detail))
    print("-" * 66)
    print("PASS %d / FAIL %d" % (n_pass, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
