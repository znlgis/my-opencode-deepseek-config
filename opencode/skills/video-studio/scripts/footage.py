# -*- coding: utf-8 -*-
"""footage.py — 素材剪辑生产线（footage 模式）的一条龙入口。

    python footage.py --footage footage.json --work work --out out/film.mp4 --stage all

与零素材线的关系
----------------
render.py 是"每一帧由代码画出来"的生产线（画面 100% 可控、可复现、零素材）。
本脚本是"画面来自现成素材"的生产线：素材可以是网上下载的视频片段 / 电影镜头 /
图片，也可以是本地文件。两条线的**底盘完全一样**：

    TTS 实测时长 -> 时间轴 -> 采样级混音 -> loudnorm -> 质检报告

区别只在"画面从哪来"：一个来自 draw_frame(t, frame)，一个来自 sources[].at + len。

选哪条线（也是 SKILL.md 的分诊规则）
----------------------------------
  1. 用户给了素材（图片/视频/链接）            -> footage
  2. 没给素材，但网上能找到可用的画面素材      -> 先 fetch，再 footage
  3. 都找不到（或题材不该用现成画面）          -> 零素材线（render.py + design.json）

阶段（每个阶段只做自己的事，落盘后可单独重跑）
--------------------------------------------
    probe       素材体检（时长/分辨率/有无音轨）-> work/sources.json
    fetch       下载/落地素材 -> dl/ 或 sources[].path
    transcribe  转写 -> work/transcripts/*.json（定位镜头用；可 --allow-missing-asr 跳过）
    locate      关键词检索 -> 打印候选时间码（据此填 lines[].shot.at）
    tts         逐句旁白 + 实测时长 -> work/tts/、work/tts.json
    edl         时间轴 + EDL -> work/timeline.json、work/edl.json（越界镜头在这里拦住）
    cut         按 EDL 切镜（统一画幅 + 抽原声）-> work/segs/
    mix         三条总线混音 -> work/mix.wav
    assemble    拼接 + 字幕 + loudnorm -> 成片
    qa          F1..F9 专属门禁 + 通用质检 -> out/QC-REPORT-footage.md
    all         出片所需的阶段按依赖顺序跑完（素材已在本地时自动跳过 fetch）

--stage 之间是硬依赖：缺前置产物会明确说缺哪个、该先跑哪个，而不是半路炸在 ffmpeg 里。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filmkit      # noqa: E402
import footageedit  # noqa: E402
import footagekit   # noqa: E402


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _load(args):
    return footagekit.load_footage(args.footage)


def _need(path, hint):
    p = Path(path)
    if not p.exists():
        raise SystemExit("缺 %s：先跑 --stage %s" % (p, hint))
    return p


def _write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ============================================================================
# stages
# ============================================================================

def st_probe(foot, work, args):
    log("== probe 素材体检")
    info = footagekit.probe_all(foot)
    _write_json(work / "sources.json", info)
    for sid, rec in info.items():
        if rec.get("missing"):
            continue
        sug = footagekit.suggest_treatment(rec)
        if sug.get("crop"):
            log("  [treatment] %s 建议 crop=%s（%s）" % (sid, sug["crop"], sug["reason"]))
            if sug.get("burned_subs") or sug.get("watermark"):
                # 把要粘进 footage.json 的声明打出来：F2/F3 是几何判定，
                # 没有这些声明就只能记 skipped（"没查"不等于"通过"）
                log("     sources[] 里补上：\"burned_subs\": %s, \"watermark\": %s"
                    % (json.dumps(sug.get("burned_subs"), ensure_ascii=False),
                       json.dumps(sug.get("watermark"), ensure_ascii=False)))
    return info


def st_fetch(foot, work, args):
    log("== fetch 素材下载/落地")
    res = footagekit.fetch_all(foot, force=args.force)
    _write_json(work / "fetch.json", res)
    return res


def st_transcribe(foot, work, args):
    log("== transcribe 转写")
    info = _load_json(work / "sources.json") if (work / "sources.json").exists() else {}
    out = {}
    for sid, src in footagekit.source_map(foot).items():
        path = footagekit.source_path(foot, src)
        if not path.exists():
            log("  [skip] %s 没有本地文件（先 --stage fetch）" % sid)
            continue
        rec = info.get(sid) or footagekit.probe_file(path)
        if rec.get("is_image") or rec.get("is_audio_only"):
            log("  [skip] %s 是图片/纯音频，无需转写" % sid)
            continue
        out[sid] = footagekit.transcribe(path, work, model=args.model, lang=args.lang,
                                         force=args.force,
                                         allow_missing=args.allow_missing_asr,
                                         source_id=sid)
    _write_json(work / "transcripts" / "index.json",
                {k: {"segs": len(v.get("segs") or []), "dur": v.get("dur"),
                     "enabled": v.get("enabled")} for k, v in out.items()})
    return out


def st_locate(foot, work, args):
    log("== locate 关键词定位")
    if not args.keywords:
        raise SystemExit("--stage locate 需要 --keywords（逗号分隔，如 茅台,娄山关）"
                         "  —— 关键词走 Python 内部字符串，不要经 shell 参数（Windows 控制台 GBK 会乱码）")
    tdir = work / "transcripts"
    if not tdir.exists():
        raise SystemExit("缺转写目录：先跑 --stage transcribe")
    trs = []
    for p in sorted(tdir.glob("*.json")):
        if p.name == "index.json":
            continue
        d = _load_json(p)
        if d.get("segs"):
            trs.append(d)
    if not trs:
        raise SystemExit("没有可用的转写结果（faster-whisper 没装或没跑成功）")
    kws = [k.strip() for k in str(args.keywords).replace("，", ",").split(",") if k.strip()]
    report = {"keywords": kws, "results": {}}
    for d in trs:
        name = str(d.get("source_id") or Path(d.get("src") or "src").stem)
        hits = footagekit.locate(d, kws, n=args.n)
        report["results"][name] = hits
        log("\n### %s" % name)
        for line in footagekit.locate_report(hits, kws).splitlines():
            log("  " + line)
    _write_json(work / "locate.json", report)
    log("\n定位结果已写 work/locate.json；填进 lines[].shot（source + at）后跑 --stage edl")
    return report


def st_tts(foot, work, args):
    log("== tts 旁白")
    audio = foot.get("audio") or {}
    rows = filmkit.tts_batch(foot["lines"], str(work),
                             voice=audio.get("voice") or "zh-CN-YunyangNeural",
                             rate=audio.get("rate") or "+0%",
                             pitch=audio.get("pitch") or "+0Hz",
                             workers=int(audio.get("workers") or 4),
                             trim=bool(audio.get("trim_silence", True)),
                             words=bool(audio.get("words", True)))
    _write_json(work / "tts.json", rows)
    # 每句的 mp3 必须真的落地：tts_batch 只保证"合成调用返回了"，文件写失败/被清理时
    # 它不会替我们发现。等 mix 阶段才发现缺一句，代价是整片重跑；这里先点名。
    missing = [str(ln["id"]) for ln in foot["lines"]
               if not (Path(work) / "tts" / (str(ln["id"]) + ".mp3")).is_file()]
    if missing:
        raise SystemExit("这些句子的旁白文件没生成：%s（看上面 tts 的报错；重跑 --stage tts）"
                         % ", ".join(missing))
    log("  旁白总时长 %.2fs / %d 句" % (sum(float(r["dur"]) for r in rows), len(rows)))
    return rows


def _load_tts(work, foot):
    p = Path(work) / "tts.json"
    if not p.exists():
        raise SystemExit("缺 work/tts.json：先跑 --stage tts")
    rows = _load_json(p)
    known = {str(r["id"]) for r in rows}
    missing = [str(l["id"]) for l in foot["lines"] if str(l["id"]) not in known]
    if missing:
        raise SystemExit("tts.json 缺这些句子：%s（改过台词？重跑 --stage tts）"
                         % ", ".join(missing))
    return rows


def st_edl(foot, work, args):
    log("== edl 时间轴 + 剪辑清单")
    tts_rows = _load_tts(work, foot)
    tl = footageedit.build_timeline(foot, tts_rows)
    _write_json(work / "timeline.json", tl)
    log("  时间轴 %.2fs（%d 句）" % (tl["total"], len(tl["lines"])))
    _budget = footagekit.source_budget(tl["total"])
    _have = len(footagekit.source_map(foot))
    log("  素材预算：%.0fs -> 推荐 %d 个独立来源（硬下限 %d）；当前声明 %d 个"
        % (_budget["total_sec"], _budget["recommended"], _budget["minimum"], _have))
    srcs_info = {}
    sp = work / "sources.json"
    if sp.exists():
        srcs_info = _load_json(sp)
    edl, problems = footageedit.make_edl(foot, tl, sources_info=srcs_info)
    _write_json(work / "edl.json", edl)
    if problems:
        log("  [F1] EDL 发现 %d 个问题：" % len(problems))
        for p in problems:
            log("    - %s" % p)
        if args.strict_edl:
            raise SystemExit("EDL 有问题（--strict-edl）：修好 lines[].shot 再跑")
    else:
        log("  [F1] EDL 通过（%d 镜 / %.2fs）"
            % (len(edl["shots"]), sum(s["len"] for s in edl["shots"])))
    # 素材量闸（与 F8 同一口径，但**提前**到切镜之前）：切了 40 个镜头再发现
    # "只有 2 个源"就等于白cut一遍。默认 fail-closed，显式 --allow-sparse-sources 才继续。
    usage = footagekit.source_usage(edl)
    if usage["distinct"] < _budget["minimum"]:
        msg = ("素材太少：%d 个独立来源 / %.0fs（硬下限 %d、推荐 %d）。\n"
               "  画面会全程是同一张图在平移。补素材，或加 --allow-sparse-sources 显式放行\n"
               "  （F8 会把它记成 fail，不会因为放行就变成 pass）"
               % (usage["distinct"], _budget["total_sec"],
                  _budget["minimum"], _budget["recommended"]))
        if args.allow_sparse_sources:
            log("  [F8] WARN " + msg)
        else:
            raise SystemExit("[F8] " + msg)
    elif usage["distinct"] < _budget["recommended"]:
        log("  [F8] 提示：%d 个来源低于推荐 %d，画面重复度会偏高"
            % (usage["distinct"], _budget["recommended"]))
    return {"timeline": tl, "edl": edl, "problems": problems}


def st_cut(foot, work, args):
    log("== cut 切镜")
    edl = _load_json(_need(work / "edl.json", "edl"))
    rows = footageedit.cut_shots(foot, edl, work, crf=int(foot.get("crf", 16)),
                                 preset=str(foot.get("preset", "veryfast")))
    _write_json(work / "cut.json", rows)
    return rows


def st_mix(foot, work, args):
    log("== mix 混音")
    tl = _load_json(_need(work / "timeline.json", "edl"))
    cut = _load_json(_need(work / "cut.json", "cut"))
    tts_rows = _load_tts(work, foot)
    return footageedit.mix_tracks(tts_rows, tl, cut, foot, work)


def st_assemble(foot, work, args):
    log("== assemble 出片")
    tl = _load_json(_need(work / "timeline.json", "edl"))
    cut = _load_json(_need(work / "cut.json", "cut"))
    ass = footageedit.write_ass(tl, foot, work / "sub.ass")
    silent = footageedit.concat_video(cut, work)
    out = footageedit.assemble(foot, work, args.out, ass_path=ass["path"],
                               crf=int(foot.get("crf", 19)),
                               preset=str(foot.get("preset", "medium")))
    log("  [done] %s" % out)
    return {"out": out, "silent": silent, "ass": ass}


def st_qa(foot, work, args):
    log("== qa 质检")
    import footageqa
    out = Path(args.out)
    if not out.exists():
        raise SystemExit("找不到成片：先跑 --stage assemble")
    tl = _load_json(work / "timeline.json") if (work / "timeline.json").exists() else {"lines": []}
    edl = _load_json(work / "edl.json") if (work / "edl.json").exists() else {"shots": []}
    return footageqa.run_all(foot, work, out, tl, edl, strict=args.strict_qa)


STAGES = {
    "probe": st_probe, "fetch": st_fetch, "transcribe": st_transcribe, "locate": st_locate,
    "tts": st_tts, "edl": st_edl, "cut": st_cut, "mix": st_mix,
    "assemble": st_assemble, "qa": st_qa,
}
ORDER = ["probe", "fetch", "transcribe", "tts", "edl", "cut", "mix", "assemble", "qa"]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="素材剪辑生产线：下载/本地素材 -> 旁白 -> 时间轴 -> 切镜 -> 混音 -> 成片 -> 门禁",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--footage", default="footage.json", help="契约文件（默认 footage.json）")
    ap.add_argument("--work", default="work", help="中间产物目录（默认 work）")
    ap.add_argument("--out", default="out/film.mp4", help="成片路径（默认 out/film.mp4）")
    ap.add_argument("--stage", default="all",
                    choices=list(STAGES.keys()) + ["all"], help="只跑某个阶段（默认 all）")
    ap.add_argument("--force", action="store_true", help="忽略缓存（fetch / transcribe / tts）")
    ap.add_argument("--model", default="small", help="转写模型（默认 small）")
    ap.add_argument("--lang", default="zh", help="转写语种（默认 zh）")
    ap.add_argument("--allow-missing-asr", action="store_true",
                    help="没有 faster-whisper 也继续（定位退化为手填时间码）")
    ap.add_argument("--keywords", default="", help="locate 用：逗号分隔的关键词")
    ap.add_argument("--n", type=int, default=12, help="locate 返回的候选条数")
    ap.add_argument("--strict-edl", action="store_true", help="EDL 有问题直接失败")
    ap.add_argument("--strict-qa", action="store_true", help="F 门禁有 fail 直接失败")
    ap.add_argument("--allow-sparse-sources", action="store_true",
                    help="素材数低于硬下限也继续（会在 QC 报告里记 F8 fail 的旁路说明）")
    ap.add_argument("--allow-missing-sources", action="store_true",
                    help="有源下载失败也继续（默认 fail-closed：缺素材就别往下切）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001  —— 老终端不支持时不阻塞
        pass

    t0 = time.time()
    foot = _load(args)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    args.out = str(out_path)

    log("footage  %s" % foot.get("title"))
    log("工作目录 %s" % work)

    summary, stages = {}, (ORDER if args.stage == "all" else [args.stage])
    for name in stages:
        if args.stage == "all":
            # all 只跑出片必需的阶段：下载与转写是准备动作，素材/定位已完成时不该重跑
            if name == "fetch":
                missing = [sid for sid, s in footagekit.source_map(foot).items()
                           if not footagekit.source_path(foot, s).exists()]
                if not missing:
                    log("== fetch 跳过（素材都在本地）")
                    continue
                log("== fetch 需要下载 %d 个源：%s" % (len(missing), ", ".join(missing)))
            if name in ("transcribe", "locate"):
                log("== %s 跳过（all 只跑出片必需阶段；定位请单独 --stage locate）" % name)
                continue
        t1 = time.time()
        summary[name] = STAGES[name](foot, work, args)
        log("-- %s 完成（%.1fs）" % (name, time.time() - t1))
        if name == "fetch":
            still = [sid for sid, s in footagekit.source_map(foot).items()
                     if not footagekit.source_path(foot, s).exists()]
            if still:
                msg = ("还有 %d 个源没落地：%s\n"
                       "  素材不全就往下切＝成片少画面。修复素材或加 --allow-missing-sources"
                       % (len(still), ", ".join(still)))
                if args.allow_missing_sources:
                    log("  [fetch] WARN " + msg)
                else:
                    raise SystemExit("[fetch] " + msg)

    dt = time.time() - t0
    payload = {"title": foot.get("title"), "stage": args.stage, "work": str(work),
               "out": args.out, "cost_s": round(dt, 1), "stages": list(summary.keys())}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    log("全部完成（%.1fs）" % dt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
