# -*- coding: utf-8 -*-
"""stagewatch.py — 阶段新鲜度（stale）守卫。

解决的是"改了一句台词，旧产物还在用，但不报错"这一类事故。

已经在 `_frames_plan_stamp` 里为一个点做过（改台词 -> 旧分块复用 -> 画面旁白对不上），
但其余 stage 全是裸奔：
  * 改 style 后 `--stage audio` 仍读上一轮的 work/music.wav（配乐是旧的）；
  * 改 hold 后 `--stage frames` 仍用旧 cues.json（音效落位错）；
  * 单独跑 `--stage audio` 会无条件混入已存在的 work/sfx.wav。

机制：每个 stage 结束时往 `work/manifest.json` 记一行
    {stage, at, inputs: {name: 指纹}, outputs: {name: 指纹}}
下次该 stage 开始前，重算**上游 inputs** 与**自己 outputs** 的指纹：
  * 上游变了 -> 本 stage 的输入过时（会拿旧输入算出新产物）；
  * 自己的 outputs 变了 -> 下游全部过时。
两者都只**警告 + 给出最小重跑命令**，默认不阻断（可用 --strict-stale 升级为失败，
--allow-stale 显式放行并留痕）。这是"门禁可以保守，但不能静默"的同一条原则。

指纹策略：小文件用 sha1（准），大文件用 size+mtime（快）。
100 MB 的 audio.wav 每 stage 都算 sha1 会让"跑一次 qa"变慢一倍，而它的目的只是
"发现它被人动过"，size+mtime 足够。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

SCHEMA = 1
BIG_BYTES = 8 << 20          # > 8 MB 走 size+mtime 快路径

# 每个 stage 的输入与输出（相对 work/ 或绝对路径）。
# 顺序即依赖顺序：后面的 stage 用前面 stage 的产物。
# `sources` 是**源头输入**：剧本（film.json）与画面代码（scenes.py）。
# 早期版本只跟踪"stage 之间的产物"，于是"改了一句台词但没重跑 tts"查不出任何过时——
# 而那正是最需要它报警的场景（旧 tts 配新剧本 = 配音和字幕全错位）。
STAGE_IO = {
    # voice_plan.json 是 **tts** 阶段写的（render.py 的 st_tts：非 legacy 才写），
    # 不是 style 写的——挂错到 style.outputs 下，单跑 `--stage style`
    # 之后每次重跑都报「产物 voice_plan.json 不见了」（其实从来没写过），
    # 开了 --strict-stale 就直接失败，而 --stage style 是全新项目的第一条生产命令。
    # legacy 剧本本来就不写这个文件：check() 只在「上次记录的是具体指纹、现在没了」
    # 时才报 output-missing，所以 legacy 下记为 missing 不会被当成永久过时。
    # 提案层：proposal.json / design.json 由 propose.py 写（不经 render.py），
    # 但**style 与 frames 都必须读它**——所以挂在 style 的 sources 里当"源头输入"，
    # 并在 frames 的 inputs 里再要一次 design.json（改设计 = 画面必须重渲）。
    # 为什么放 sources 而不是 inputs：它不是 render.py 的任何 stage 产出的，
    # 按 inputs 记会永远报"产物缺失"（proposal 还没跑时）。
    "style":   dict(sources=["script", "design"], inputs=[], outputs=["style.json"]),
    "tts":     dict(sources=["script"], inputs=["style.json"],
                    outputs=["tts.json", "voice_plan.json"]),
    "layout":  dict(sources=["script"],
                    inputs=["tts.json", "style.json", "voice_plan.json"],
                    outputs=["timeline.json", "sub.srt", "sub.ass"]),
    "cues":    dict(sources=["script", "scenes"], inputs=["timeline.json"],
                    outputs=["cues.json"]),
    "score":   dict(sources=["script"], inputs=["timeline.json", "style.json"],
                    outputs=["music.wav", "score.json"]),
    "sfx":     dict(sources=["script"], inputs=["cues.json"], outputs=["sfx.wav"]),
    "audio":   dict(sources=["script"],
                    inputs=["tts.json", "timeline.json", "music.wav", "sfx.wav"],
                    outputs=["audio.wav"]),
    "frames":  dict(sources=["script", "scenes", "design"],
                    inputs=["timeline.json", "audio.wav", "cues.json"],
                    outputs=["silent.mp4"]),
    "mux":     dict(sources=["script"], inputs=["silent.mp4", "audio.wav"], outputs=[]),
    # gates.json 不列进 qa 的产物：它由 _finish_gates 在 style/frames/mux/qa 任何一次收尾时
    # 都会写，不能当"qa 跑过"的证据（否则删掉 qa 记录后它还在，会误判 no-manifest）。
    # qa/sheet.png（联系表）只在 --stage qa 里生成，才是真凭证。
    "qa":      dict(sources=["script", "scenes"], inputs=[],
                    outputs=["qa/sheet.png", "qa/deep.json"]),
    "stills":  dict(sources=["script", "scenes"], inputs=["timeline.json", "style.json"],
                    outputs=[]),
}
STAGE_ORDER = tuple(STAGE_IO)

# 每个 stage 过时后的"最小重跑命令"提示（只提示本 stage + 必要的下游）
RERUN_HINT = {
    "style":   "python render.py --stage style tts layout cues score sfx audio frames mux qa",
    "tts":     "python render.py --stage tts layout cues sfx audio frames mux qa",
    "layout":  "python render.py --stage layout cues score sfx audio frames mux qa",
    "cues":    "python render.py --stage cues sfx audio frames mux qa",
    "score":   "python render.py --stage score audio mux qa",
    "sfx":     "python render.py --stage sfx audio mux qa",
    "audio":   "python render.py --stage audio mux qa",
    "frames":  "python render.py --stage frames mux qa  （分块缓存会被指纹拒绝，需 rm -rf work/segments）",
    "mux":     "python render.py --stage mux qa",
    "qa":      "python render.py --stage qa",
    "stills":  "python render.py --stage stills",
}


def _fp(path, work):
    """文件指纹：缺失 -> 'missing'；>8MB 用 size+mtime（快），否则 sha1（准）。"""
    p = path if os.path.isabs(str(path)) else os.path.join(str(work), str(path))
    try:
        st = os.stat(p)
    except OSError:
        return "missing"
    if st.st_size > BIG_BYTES:
        return "sm:%d:%d" % (st.st_size, int(st.st_mtime))
    try:
        h = hashlib.sha1()
        with open(p, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return "sha1:" + h.hexdigest()[:16]
    except OSError:
        return "missing"


def manifest_path(work):
    return os.path.join(str(work), "manifest.json")


def load(work):
    p = manifest_path(work)
    if not os.path.exists(p):
        return {"schema": SCHEMA, "stages": {}}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema": SCHEMA, "stages": {}, "broken": p}
    if not isinstance(d, dict) or not isinstance(d.get("stages"), dict):
        return {"schema": SCHEMA, "stages": {}, "broken": p}
    return d


def save(work, d):
    os.makedirs(str(work), exist_ok=True)
    p = manifest_path(work)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def _producer_of(art):
    """work/ 里的产物文件是哪个 stage 写的（由 STAGE_IO 反查，找不到返回 None）。"""
    for st, io in STAGE_IO.items():
        if art in (io.get("outputs") or []):
            return st
    return None


# 硬类型：这些永远参与 --strict-stale 判定（产物丢了 / 源头改了 / 上游自己就过时）
HARD_KINDS = ("source-changed", "output-missing", "no-manifest", "input-stale")
SOFT_KINDS = ("input-pending", "source-pending")


SRC_LABEL = {"script": "剧本 film.json",
             "scenes": "画面代码 scenes.py",
             "design": "设计 design.json（propose.py --confirm 冻结的那份）"}


def _src_fp(kind, sources):
    """源头输入指纹：script=剧本、scenes=场景代码、design=冻结设计。"""
    p = (sources or {}).get(kind)
    if not p:
        return "missing"
    return _fp(p, ".")


def _src_label(kind):
    return SRC_LABEL.get(str(kind), str(kind))


def snapshot(work, stage, sources=None):
    """记下某个 stage 的输入/输出指纹（stage 结束时调用）。"""
    io = STAGE_IO.get(str(stage))
    if io is None:
        return None
    d = load(work)
    d.setdefault("stages", {})[str(stage)] = dict(
        at=time.time(),
        atText=time.strftime("%Y-%m-%d %H:%M:%S"),
        src={k: _src_fp(k, sources) for k in (io.get("sources") or [])},
        inputs={k: _fp(k, work) for k in io["inputs"]},
        outputs={k: _fp(k, work) for k in io["outputs"]},
    )
    save(work, d)
    return d["stages"][str(stage)]


def _upstream_hard(work, stage, sources=None, _seen=None):
    """产出某输入的 stage 是否有**硬**问题（源头变了 / 它的输入也变了 / 产物丢了）。

    只取硬类型：软化过的 input-pending 不算（那只说明"它也会重跑"）。
    递归带上 _seen 防环——STAGE_IO 是 DAG，但用户手改的 manifest 不该把检查器搞成死循环。
    """
    if not stage:
        return []
    issues = check(work, stage, sources, pending=(), _seen=_seen)
    return [it["reason"] for it in issues if it.get("kind") in HARD_KINDS]


def check(work, stage, sources=None, self_built=False, pending=(), _seen=None):
    """stage 开始前查新鲜度：返回问题列表（每项含 stage / reason / rerun）。

    pending: 本次命令行**已经计划要跑**的 stage 集合（render.py 传 args.stage 展开后的列表）。
      对本 stage 来说，"自己上次跑完之后上游变了" + "本轮马上就要重跑自己" = 正常待办，
      不是过时（`--stage frames mux` 里 frames 刚重出 silent.mp4，mux 报"输入变了"是自相矛盾）。
      这类问题标 kind="input-pending"、soft=True：照实打印，但不参与 --strict-stale 判定。
      真问题不受影响：上游 stage **自己**过时（它的输入也变了）仍按 input-stale/output-missing 硬报，
      所以"单独跑 mux、上游 frames 早过时"照样失败。

    只报"这一跑真的会读到旧东西"的情况：
      1) 本 stage 的上游输入与上次记录不同（上游被重跑过，或人手改过文件）；
      2) 本 stage 自己的产物比它的输入还旧（上次跑到一半被中断）。
    另一个 stage 的产物变了但**不**是本周 stage 的输入时，不报（跟我们无关）。

    self_built: 这些输入是**本次运行自己刚写出来的**（render.py --stage all 跑在
    一个刚建的 work/ 上）。此时"manifest 没有本阶段记录"只是因为我们还没跑到它，
    不是旧产物；报 no-manifest 等于让全新目录的首跑刷一屏假警报。
    """
    io = STAGE_IO.get(str(stage))
    if io is None:
        return []
    _seen = set(_seen or ())
    if str(stage) in _seen:
        return []
    _seen.add(str(stage))
    d = load(work)
    stages = d.get("stages") or {}
    rec = stages.get(str(stage))
    planned = set(str(x) for x in (pending or ()))
    issues = []
    if not isinstance(rec, dict):
        # 从没记录过这个 stage。判据必须能区分两件事：
        #   * 「还没跑到它」——分步跑（SKILL.md 主推：--stage style / tts / layout / …）时，
        #     刚跑完 style，tts 的输入 style.json 当然存在、manifest 里当然没有 tts 记录。
        #     这**不是**过时，按"输入齐了"报会把这套标准用法整个打成红（--strict-stale 下必然失败）。
        #   * 「跑过它但查不到记录」——旧版本产物 / 手改 / 复制来的 work 目录。
        # 区分证据：本 stage **自己声明的产物**已经在 work/ 里。产物在、记录没有，才证明它跑过。
        # 输入齐全不能当证据（分步跑时本来就齐），输出存在才可以。
        present_out = [k for k in (io.get("outputs") or []) if _fp(k, work) != "missing"]
        # stills / mux 的产物不在 work/（mux 写 --out，stills 写 work/stills/），无从证明跑过，
        # 不冤枉它们；self_built（本次新建的 work/）连 manifest 都刚建，也不报。
        if not self_built and stages and present_out:
            issues.append(dict(stage=str(stage), kind="no-manifest",
                               reason="work 里已经有本阶段的产物（%s），但 manifest.json 没有本阶段的记录"
                                      "（旧版本跑过 / 手改过）" % "、".join(present_out),
                               rerun=RERUN_HINT.get(str(stage), "")))
        return issues
    # 源头先查：剧本 / scenes.py 改了但本 stage 没重跑 -> 产物一定过时
    old_src = rec.get("src") or {}
    for kind in (io.get("sources") or []):
        now = _src_fp(kind, sources)
        was = old_src.get(kind, "missing")
        if now == was:
            continue
        fname = _src_label(kind)
        if str(stage) in planned:
            # 源头变了 + 本轮马上就要重跑本阶段 = 正常待办，不是过时。
            # 不软化的话，「改一句台词 -> --stage all --strict-stale」会在第一个 stage
            # 直接失败（"没重跑却装了旧内容"——其实马上就要重跑），把主工作流堵死；
            # 与 inputs 的 input-pending 是同一套语义，只是源头走的是另一个循环。
            issues.append(dict(
                stage=str(stage), kind="source-pending", file=kind, soft=True,
                reason="%s 在上次跑本阶段之后变了（%s -> %s）；本阶段本轮会重跑，"
                       "按新内容重算" % (fname, was, now),
                rerun=RERUN_HINT.get(str(stage), ""),
            ))
            continue
        issues.append(dict(
            stage=str(stage), kind="source-changed", file=kind,
            reason="%s 在本阶段上次跑完之后改过（%s -> %s），但本阶段没有重跑；"
                   "它现在装的还是旧内容" % (fname, was, now),
            rerun=RERUN_HINT.get(str(stage), ""),
        ))
    old_inputs = rec.get("inputs") or {}
    for name in io["inputs"]:
        now = _fp(name, work)
        was = old_inputs.get(name, "missing")
        if now == was:
            continue
        if str(stage) in planned:
            # 本阶段本轮就要重跑：拿新输入重算正是它该干的事。照实打一行（不静默），
            # 但不参与 strict 判定——否则 `--stage frames mux` 这种 stagewatch 自己建议的
            # 重跑组合，会在 mux 开跑前被自己拦下，strict 下永远出不了片。
            issues.append(dict(
                stage=str(stage), kind="input-pending", file=name, soft=True,
                reason="输入 %s 在上次跑本阶段之后变了（%s -> %s）；本阶段本轮会重跑，"
                       "按新输入重算" % (name, was, now),
                rerun=RERUN_HINT.get(str(stage), ""),
            ))
            continue
        # 不由本阶段重算时：还要确认产出这个输入的 stage **自己**没坏。
        # 上游自己就过时（源头改了 / 它的输入也变了 / 它的产物丢了），那这台旧货是真的不能用。
        prod = _producer_of(name)
        ups = _upstream_hard(work, prod, sources, _seen) if prod else []
        if ups:
            issues.append(dict(
                stage=str(stage), kind="input-stale", file=name,
                reason="输入 %s 变了（%s -> %s），并且产出它的 %s 阶段本身就已经过时：%s"
                       % (name, was, now, prod, "；".join(ups[:3])),
                rerun=RERUN_HINT.get(str(stage), ""),
            ))
        else:
            issues.append(dict(
                stage=str(stage), kind="input-changed", file=name,
                reason="输入 %s 在上次跑本阶段之后变了（%s -> %s），本次会拿**新输入**算，"
                       "但它下游的产物仍是旧的" % (name, was, now),
                rerun=RERUN_HINT.get(str(stage), ""),
            ))
    # 自己的产物丢了：上次跑到一半被中断 / 被删 / 被挪走（与"输入变了"是两件事，都要报）。
    # 只报「上次记录到真实指纹、现在没了」的文件：有些产物是**条件性**的（legacy 剧本
    # 不写 voice_plan.json），它们上次就记为 missing，现在还是 missing，那是正常状态；
    # 一律按"现在是 missing 就报"会让这类文件变成永久误报（--strict-stale 下直接失败）。
    rec_out = rec.get("outputs") or {}
    missing_out = [k for k, was in rec_out.items()
                   if was not in (None, "missing") and _fp(k, work) == "missing"]
    if missing_out:
        issues.append(dict(stage=str(stage), kind="output-missing",
                           reason="上次跑过本阶段，但产物 %s 不见了（被删 / 被挪 / 磁盘满）"
                                  % "、".join(missing_out),
                           rerun=RERUN_HINT.get(str(stage), "")))
    return issues


def explain(work, stages=None, sources=None, self_built=False, pending=()):
    """--explain-stale：把"现在哪些阶段是旧的、为什么、该跑什么"一次说清。

    pending: 本次命令行要跑的 stage 集合（见 check）；--stage explain 不传 = 全按"不重跑"判。
    """
    d = load(work)
    rows = []
    for st in (stages or STAGE_ORDER):
        io = STAGE_IO.get(st)
        if io is None:
            continue
        rec = (d.get("stages") or {}).get(st)
        issues = check(work, st, sources, self_built=self_built, pending=pending)
        rows.append(dict(stage=st, recorded=bool(rec),
                         at=(rec or {}).get("atText", ""),
                         issues=issues))
    return rows


def report(work, stages=None, strict=False, allow_stale=False, ledger=None, write=None,
           sources=None, self_built=False, pending=()):
    """查 + 打印 + 判定。返回 (errors, warnings)；errors 在 strict 下非空。

    ledger: 传 gates.GateLedger 时把结论记进账本（一条汇总 gate）。
    write:  传 callable(str, str) 时按行输出（方便测试捕获）。
    self_built: 见 check()——本次运行的 work/ 是刚建的，别报"没有记录=过时"。
    pending: 本次命令行要跑的 stage 集合（见 check）。其中"本阶段会重算"的输入变化
             标 soft，照实打印但不参与 strict 判定。
    """
    out = write or (lambda level, msg: print(msg))
    rows = explain(work, stages, sources, self_built=self_built, pending=pending)
    problems = [(r["stage"], i) for r in rows for i in r["issues"]]
    warns, errs = [], []
    for st, it in problems:
        soft = bool(it.get("soft")) or it.get("kind") in SOFT_KINDS
        # 两类不要用同一个前缀：soft 是"本轮会重算"的待办，不是过时。用 [stale] 会把
        # 人吓一跳，也会让"grep [stale] 判红"的 CI 配方误报。
        msg = (("[待重算] %s: %s" if soft else "[stale] %s: %s") % (st, it["reason"]))
        if soft:
            # 本轮会重算的输入变化：照实说，但不算失败。--strict-stale 也不拦。
            warns.append(msg)
        elif allow_stale:
            warns.append(msg + "（--allow-stale 已放行，本次结果可能掺旧数据）")
        elif strict:
            errs.append(msg)
        else:
            warns.append(msg)
        # 原因行必须打出来：调用方（render.py --stage all）只显示"最小重跑"，
        # 用户会看到"共 1 处过时"却不知道是哪个 stage、为什么——这正是本模块
        # 开头承诺的"可以保守，但不能静默"。返回的 warns/errs 是给调用方判定的，
        # 不代替人读的输出。
        out(("warn" if soft else "err") if (strict and not allow_stale) else "warn", msg)
        if it.get("rerun") and not soft:
            out("warn", "   最小重跑: %s" % it["rerun"])
    hard = [(st, it) for st, it in problems
            if not (it.get("soft") or it.get("kind") in SOFT_KINDS)]
    if problems:
        out("warn", "")
        if hard:
            out("warn", "   ↑ 共 %d 处过时。默认只告警；--strict-stale 让它失败，"
                        "--allow-stale 显式放行。放行会记进本次门禁账本，"
                        "交付阶段（qa / mux / all / stills / style）会体现在 QC 报告里。"
                        % len(hard))
        else:
            out("warn", "   ↑ 上面 %d 条都是「本轮会重算」的输入变化（不是过时，不拦 strict）。"
                        % len(problems))
    if ledger is not None:
        try:
            if not hard:
                ledger.ok("stale", "阶段新鲜度",
                          "上游输入与上次记录一致" if not problems
                          else "%d 处输入会在本轮重算，无需拦" % len(problems))
            elif errs:
                ledger.fail("stale", "阶段新鲜度", "%d 处过时（--strict-stale）" % len(hard))
            else:
                ledger.skip("stale", "阶段新鲜度",
                            "%d 处过时，已告警未阻断%s"
                            % (len(hard), "（--allow-stale）" if allow_stale else ""))
        except Exception:                                   # noqa: BLE001
            pass
    return errs, warns


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="阶段新鲜度检查：现在哪些产物是旧的")
    ap.add_argument("--work", default="work")
    ap.add_argument("--stage", default="", help="只看某个 stage（缺省 = 全部）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    stages = [a.stage] if a.stage else None
    rows = explain(a.work, stages)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        sys.exit(0)
    bad = 0
    for r in rows:
        mark = "OK " if not r["issues"] else "!! "
        if r["issues"]:
            bad += 1
        print("%s%-8s %s" % (mark, r["stage"], r["at"] or "（无记录）"))
        for it in r["issues"]:
            print("     -", it["reason"])
            if it.get("rerun"):
                print("       最小重跑:", it["rerun"])
    print()
    if bad:
        print("%d 个阶段过时。只想看某个：--stage <名字>；打印机读：--json" % bad)
        sys.exit(1)
    print("没有发现过时的阶段（按 manifest.json 记录判断）。")
