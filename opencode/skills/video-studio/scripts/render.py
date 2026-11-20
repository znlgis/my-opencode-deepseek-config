# -*- coding: utf-8 -*-
"""render.py — 一条命令从剧本到成片。

    python render.py --script film.json --scenes scenes.py --out out/film.mp4 [--stage all]

阶段（每个 stage 只做自己的事，不顺手重渲全片）：
    style   解析设计 -> work/style.json（画风/配乐/旁白/节奏一次定死）
    tts     逐句合成旁白 + 实测时长（带输入校验的缓存；按声线计划换人换语气）
    layout  由实测时长排时间轴 -> timeline.json / sub.srt / sub.ass（含停留窗口 hold）
    cues    画面提示点 -> work/cues.json（web 引擎从页面导出，pil 引擎从 film.json 生成）
    score   参数化配乐 -> work/music.wav（musicgen；缺了直接报错，不再回退模板引擎）
    sfx     按提示点合成音效轨 -> work/sfx.wav（sfxkit；无则跳过）
    audio   三总线混音（旁白 + 配乐 + 音效，音效响起时配乐让位）-> audio.wav
    frames  逐帧渲染 -> silent.mp4（--engine pil|web；分块并行、断点续跑）
    mux     合成（视频流 copy，两遍 loudnorm）
    qa      质检 + 抽帧 + 联系表 + 帧级深检（闪烁/空窗/音画同步）
    stills  只渲染若干关键帧做母版预览（不生成视频）
    accept  人工签核一道闸（--gate style|stills|facts|release），写 work/signoff.json
    explain 打印"现在哪些 stage 是旧的、为什么、该跑什么"（--explain-stale 的入口）

依赖顺序：style -> tts -> layout -> cues -> score -> sfx -> audio -> frames -> mux -> qa。
--stage all 按顺序跑完；单跑某个 stage 时，若前置产物缺失会明确报错并提示先跑哪个。
--engine 覆盖 film.json 的 engine：pil = Pillow 逐帧（默认，依赖最少）；web = 无头浏览器逐帧。

每个 stage 都往「门禁账本」记结论（pass / fail / skipped 三档），并在
--stage qa（或 --stage all 收尾）写出 out/QC-REPORT.md：这是**机器可读的交付结论**，
而不是"agent 在对话里复述质检结果"。人工签核走 --stage accept，绑内容指纹——
改了剧本 / scenes.py / 引擎，旧签核自动失效。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _early_layout(argv=None):
    """在 import filmkit **之前**把 --layout 落到环境变量。

    为什么要在模块顶部做：`scenes.py` 写的是 `from filmkit import W, H`，而 filmkit
    的 W/H 来自 layoutkit —— 值在**导入那一刻**就绑死了。等到 argparse 解析完再设
    环境变量，此时画布早就定了，`--layout portrait` 会静默失效（最坏的那种 bug：
    用户以为在出竖屏，实际出的是横屏）。所以这里先扫一遍 argv，argparse 后面照常解析。

    只认 `--layout X` 与 `--layout=X` 两种写法；别的参数一概不碰。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    want = ""
    for i, tok in enumerate(args):
        if tok == "--layout" and i + 1 < len(args):
            want = args[i + 1]
            break
        if tok.startswith("--layout="):
            want = tok.split("=", 1)[1]
            break
    if want:
        os.environ["VIDEO_STUDIO_LAYOUT"] = want


_early_layout()

import filmkit as K
import gates as GT
import stagewatch as SW

STAGES = ("style", "tts", "layout", "cues", "score", "sfx", "audio", "frames",
          "mux", "qa", "stills", "accept", "explain", "all")

# _render_range 每帧都要主题：按 script 对象缓存一次，避免每帧重新解析（几百毫秒的浪费）
_THEME_CACHE = {}

# 本次运行的门禁账本（main() 里建；stage 函数通过它记账）
_LEDGER = None


def _gate():
    """取当前账本；被单测直接调用 stage 函数时返回 None（不记账，照常打印）。"""
    return _LEDGER


def _setup_stdio():
    """输出被管道/文件重定向时把 stdout/stderr 切到 UTF-8。

    只在"非交互"时切（isatty=False）：Windows 下 python render.py ... | tee out.log
    的 stdout 默认是 GBK 代码页，日志里中文全是乱码；真实控制台保持系统编码，
    交互运行时中文照样正常。与 musicgen / webengine 的入口处理保持同一口径。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _file_sha1(path):
    """文件内容指纹（缺文件返回 missing，不抛异常）。"""
    try:
        h = hashlib.sha1()
        with open(str(path), "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()[:16]
    except OSError:
        return "missing"


def _frames_plan_stamp(tl, script, scenes_path, chunk):
    """分块计划的指纹：时长 / scenes.py 内容 / 剧本快照 / 时间轴内容任一变化，旧分块就画出错帧。

    断点续跑只看"分块文件在不在"是危险的：改一句台词 -> 时间轴变了 -> 旧分块
    仍然被跳过复用，最后拼出一部"画面和旁白对不上"的片子却不报错。

    为什么不能只哈希 total/frames/fps：字幕窗口（voice_start/voice_end）与词级断点
    （words_abs）都在 tl["scenes"] 里，而它们可以在 total 分毫不动的情况下变化
    ——逐句被 min_dur 夹住时，重新合成的 TTS 只改句内时长、不改段落总长。只比三个
    标量会让这种情况静默复用旧分块（字幕按上一版的词断行画）。代价是每家分块计划
    多一次 timeline.json 的 json.dumps（几十 KB，相对渲一帧可忽略）。
    """
    body = json.dumps(dict(script), sort_keys=True, ensure_ascii=False, default=str)
    tl_body = json.dumps(tl, sort_keys=True, ensure_ascii=False, default=str)
    payload = dict(total=round(float(tl["total"]), 3), frames=int(tl["frames"]),
                   fps=int(tl["fps"]), chunk=int(chunk),
                   timeline=hashlib.sha1(tl_body.encode("utf-8")).hexdigest()[:16],
                   scenes=_file_sha1(scenes_path),
                   script=hashlib.sha1(body.encode("utf-8")).hexdigest()[:16])
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]

# 可选模块：缺哪个就明确报错/跳过，不影响主流程
HERE_SCRIPTS = str(HERE)


def _opt_module(name, hint):
    """按需 import 同目录的可选模块；缺了给一句能照着做的提示。"""
    if HERE_SCRIPTS not in sys.path:
        sys.path.insert(0, HERE_SCRIPTS)
    try:
        return __import__(name)
    except Exception as e:                                  # noqa: BLE001
        raise SystemExit("[缺少 %s.py] %s（%s）" % (name, hint, e)) from e


def _try_module(name):
    """尽力 import 可选模块；缺了返回 None（用于"有就多做一步"的增强，不阻断主流程）。"""
    if HERE_SCRIPTS not in sys.path:
        sys.path.insert(0, HERE_SCRIPTS)
    try:
        return __import__(name)
    except Exception:                                       # noqa: BLE001
        return None


def _engine(script, override=None):
    return (override or script.get("engine") or "pil").lower()


def _web_entry(script):
    """浏览器渲染的入口 HTML：film.json 的 web.entry，或项目目录下 web/index.html。"""
    web = script.get("web") or {}
    entry = web.get("entry") or os.path.join(script.get("_dir", "."), "web", "index.html")
    entry = str(Path(entry).resolve())
    if not os.path.exists(entry):
        raise SystemExit(
            "[缺少 web 页面] %s\n"
            "  web 引擎需要**你自己的页面**（本技能不提供模板）。\n"
            "  PIL 路线不需要页面，零拷贝就能出片（--engine pil）。\n"
            "  想先跑通 web 链路可拷示例页：\n"
            "    cp -r <SKILL_DIR>/assets/web ./web   # 示例页只有 3 段占位场景，不是出片模板\n"
            "  或在 film.json 里写 web.entry 指向你自己的页面。" % entry)
    return entry


def _web_where(entry):
    """报错里告诉用户 DURATION / FPS 到底写在哪：入口 HTML + 同目录脚本 + 一条能照抄的 grep。

    只报入口 HTML 不够用：模板的 window.DURATION 写在 web/film.js 开头（engine.js 另有
    兜底默认值），用户拿到 index.html 还是得全文翻。这里把候选文件一起列出来。
    """
    d = os.path.dirname(entry)
    try:
        scripts = sorted(n for n in os.listdir(d)
                         if n.lower().endswith((".js", ".mjs", ".html")))
    except OSError:
        scripts = []
    tail = ("要改的文件：%s（入口 HTML）" % entry)
    if scripts:
        tail += "；页面脚本在 %s%s" % (
            d, "（%s）" % " / ".join(scripts) if len(scripts) <= 6 else "")
    tail += "\n  查 DURATION / FPS 定义在哪：grep -n \"window.DURATION\\|global.DURATION\\|DURATION =\" %s" % d
    return tail


# ---------------------------------------------------------------- 装载
def load_module(path: str, name="film_scenes"):
    """每次都用独立模块名 + 全新 module 对象，避免跨片污染（否则会渲染出上一部片）。"""
    path = str(Path(path).resolve())
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_script(p) -> dict:
    """读剧本并做前置校验：坏 film.json 给一句能照着改的错，而不是 Python traceback。"""
    p = Path(p).resolve()
    if not p.exists():
        raise SystemExit("[缺少剧本] %s（--script 指向 film.json）" % p)
    try:
        s = json.load(open(p, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit("[剧本格式错误] %s 不是合法 JSON：%s" % (p, e)) from e
    if not isinstance(s, dict):
        raise SystemExit("[剧本格式错误] %s 顶层必须是一个 JSON 对象" % p)
    for req in ("title", "lines"):
        if req not in s:
            raise SystemExit("film.json 缺少字段: " + req)
    lines = s["lines"]
    if not isinstance(lines, list) or not lines:
        raise SystemExit("film.json 的 lines 必须是非空数组（当前是 %r）" % (lines,))
    for k in ("music", "sfx", "web"):
        if s.get(k) is not None and not isinstance(s.get(k), dict):
            raise SystemExit("film.json 的 %s 必须是对象（当前是 %s）" % (k, type(s[k]).__name__))
    # bpm 允许 "auto"（由风格梯度 + 内容指纹决定）。
    # 同时允许写在 music.bpm 里（优先级更高）。
    for _k, _v in (("bpm", s.get("bpm")),
                   ("music.bpm", (s.get("music") or {}).get("bpm"))):
        if _v in (None, "") or (isinstance(_v, str) and _v.strip().lower() == "auto"):
            continue
        try:
            if float(_v) <= 0:
                raise ValueError("必须大于 0")
        except (TypeError, ValueError) as e:
            raise SystemExit("film.json 的 %s 必须是正数或 \"auto\"（收到 %r：%s）"
                             % (_k, _v, e)) from e
    seen = set()
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            raise SystemExit("film.json 的 lines[%d] 必须是对象（当前是 %s）"
                             % (i, type(ln).__name__))
        if not str(ln.get("text") or "").strip():
            raise SystemExit("film.json 的 lines[%d] 缺少 text（朗读文本）" % i)
        ln.setdefault("id", "l%02d" % (i + 1))
        ln.setdefault("sub", ln["text"])
        sid = str(ln["id"])
        if sid in seen:
            raise SystemExit("film.json 的 lines id 重复: %r（TTS 文件与 cue 都会串）" % ln["id"])
        seen.add(sid)
    s["_path"] = str(p)
    s["_dir"] = str(p.parent)
    return s


def _to_gif(mp4, gif, fps=12, width=640):
    """mp4 -> GIF（两遍调色板，避免"一屏噪点"的默认调色板）。

    GIF 只用来"快速看一眼大概长什么样"（发给别人、贴到文档里），
    不是交付物：它没有声音、色彩也被量化。真正的交付仍是 mp4。
    """
    pal = gif + ".palette.png"
    _run_ff = K._run if hasattr(K, "_run") else None
    vf1 = "fps=%d,scale=%d:-1:flags=lanczos,palettegen=stats_mode=diff" % (fps, width)
    vf2 = "fps=%d,scale=%d:-1:flags=lanczos,paletteuse=dither=bayer:bayer_scale=3" % (fps, width)
    for args in ([K.FFMPEG, "-y", "-v", "error", "-i", str(mp4), "-vf", vf1, pal],
                 [K.FFMPEG, "-y", "-v", "error", "-i", str(mp4), "-i", pal,
                  "-lavfi", vf2, "-loop", "0", str(gif)]):
        p = subprocess.run(args, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError("GIF 生成失败: " + p.stderr.decode("utf-8", "replace")[:400])
    try:
        os.remove(pal)
    except OSError:
        pass
    return gif


def _atomic_json_retry(path, obj, tries=3, sleep_s=0.1):
    """原子写 JSON + 有限退避。

    Windows 上文件被杀毒/预览进程短暂占用时，一次写入失败会让**整片**跑挂。
    只对"可重试的写状态"退避，不对内容生成盲目重试（那会掩盖真错误）。
    """
    last = None
    for i in range(max(1, int(tries))):
        try:
            d = os.path.dirname(os.path.abspath(str(path)))
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, str(path))
            return True
        except OSError as e:                                # noqa: BLE001
            last = e
            time.sleep(sleep_s * (i + 1))
    print("   [WARN] 写 %s 失败（重试 %d 次）：%s" % (path, tries, last))
    return False


def _need(cond, msg):
    if not cond:
        raise SystemExit("[前置产物缺失] " + msg)


# ---------------------------------------------------------------- 风格解析
def _stylekit():
    """惰性 import stylekit；缺文件时给一句能照着做的错，而不是 ModuleNotFoundError。"""
    try:
        import stylekit as ST
        return ST
    except Exception as e:                                  # noqa: BLE001
        raise SystemExit("[缺少 stylekit] render.py 需要同目录的 stylekit.py"
                         "（skills/video-studio/scripts/stylekit.py）：%s" % e) from None


def _design_path(work):
    return os.path.join(str(work), "design.json") if work else ""


def _plan_from_design(script, work, verbose=True):
    """如果 work/design.json 存在（用户已确认提案），**以它为准**解析计划。

    这是唯一主路径：design.json 是"这一版长什么样"的冻结点，剧本只提供台词。
    返回 None 表示没有 design（调用方按 fail-closed 报错并给出两步指引）。
    """
    direct = script.get("design")
    design = None
    if isinstance(direct, dict):
        design = direct
    else:
        p = _design_path(work)
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    design = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                raise SystemExit("[design] %s 读不出来：%s（重新 propose.py --confirm 一次）"
                                 % (p, e)) from e
    if not isinstance(design, dict):
        return None
    try:
        import designkit as DK
    except ImportError as e:
        raise SystemExit("[design] 缺少 designkit.py（%s）——design.json 需要它解析成计划" % e) from e
    # 骨架版/半成品 designkit 会 import 成功但接口没实现：那种情况必须给一句人话，
    # 不能让 AttributeError 的 traceback 冒充"渲染器坏了"（这种误报踩过多次）。
    missing = [n for n in ("validate_design", "design_to_plan")
               if not callable(getattr(DK, n, None))]
    if missing:
        raise SystemExit("[design] designkit.py 不完整：缺 %s（这个文件还没实现完）"
                         % "、".join(missing))
    bad = DK.validate_design(design)
    if bad:
        raise SystemExit("[设计体检失败] %s\n  - %s"
                         % (design.get("id") or "(无 id)", "\n  - ".join(bad)))
    plan = DK.design_to_plan(design)
    _apply_audio_overrides(script, plan)
    if verbose:
        print("   设计: %s（%s）" % (plan.get("style"), (design.get("pitch") or "")[:60]))
    return plan


def _apply_audio_overrides(script, plan):
    """把 design.audio 里的音量/音效参数填进 script 的 music/sfx **缺省位**。

    为什么需要：design.json 里 audio.music.gain_db / duck_db 与 audio.sfx.enabled /
    density / gain_db 若只被 validate_design 接受、没有任何渲染代码读它，
    用户改了它们，成片一模一样（audit 实测：enabled=false 仍出 8 条音效）。

    优先级（明确且可预期）：**film.json 显式写了的 > design 的值 > 模块内置默认**。
    只填缺省位，绝不覆盖剧本里的显式写法——用户手里的 film.json 永远是最终裁决权。
    """
    ov = plan.get("audio_overrides") if isinstance(plan, dict) else None
    if not isinstance(ov, dict):
        return
    mus_ov = ov.get("music") if isinstance(ov.get("music"), dict) else {}
    sfx_ov = ov.get("sfx") if isinstance(ov.get("sfx"), dict) else {}
    mus = script.get("music")
    if mus_ov:
        if not isinstance(mus, dict):
            mus = {}
            script["music"] = mus
        for k, v in mus_ov.items():
            mus.setdefault(k, v)
    sfx = script.get("sfx")
    if sfx_ov:
        if not isinstance(sfx, dict):
            sfx = {}
            script["sfx"] = sfx
        for k, v in sfx_ov.items():
            sfx.setdefault(k, v)


def resolve_style(script, work=None, verbose=True):
    """解析风格计划并挂到 script["_style"] 上（同一进程内各 stage 共享）。

    优先级：
      1. script["_style"] 已存在（同进程内各 stage 复用）
      2. work/design.json（用户已确认的提案）—— 这是唯一正式主路径
      3. 都没有 -> fail-closed 报错（没有内置风格可回落）

    纯函数 + 落盘 work/style.json（审计用：用户能看到"为什么是这套风格"）。
    """
    if isinstance(script.get("_style"), dict):
        return script["_style"]
    plan = _plan_from_design(script, work, verbose=verbose)
    if plan is not None:
        script["_style"] = plan
        if work and verbose:
            try:
                os.makedirs(work, exist_ok=True)
                with open(os.path.join(work, "style.json"), "w", encoding="utf-8") as fh:
                    json.dump(plan, fh, ensure_ascii=False, indent=1)
            except Exception as e:                          # noqa: BLE001
                print("   [WARN] style.json 落盘失败：%s" % e)
        return plan
    # **没有第三优先级**：回落到"按关键词自动选风格"等于模板库，
    # 那正是本技能不提供的，所以必须明确报错，不静默回落。
    raise SystemExit(
        "[style] 找不到设计（work/design.json）：\n"
        "  本技能不提供任何内置风格，每一版外观都必须现场生成并由人确认。\n"
        "  正确做法：\n"
        "    1) python propose.py --brief \"题材/要求\" --script film.json   # 出 3 个方案\n"
        "    2) python propose.py --confirm --candidate A                   # 冻结设计\n"
        "    3) python render.py --script film.json --stage all             # 出片\n"
        "  （work=%r）" % (work,))


def _theme_for(script, work=None):
    """返回当前片的主题字典；legacy 计划返回 None（现存的兼容分支）。

    work **必须传**：design 路径的身份在 work/design.json 里，不传就直接 SystemExit ——
    但"传错 work"仍然值得防：work 指到别的目录时会读到**另一份** design.json，
    参考帧与成片不同源（实测：--stage qa 单独跑 VMAF 1.07，全部内联跑 97.15）。
    """
    plan = script.get("_style") if isinstance(script.get("_style"), dict) else None
    if plan is not None and plan.get("legacy"):
        return None
    plan = resolve_style(script, work)
    if plan.get("legacy"):
        return None
    # plan["theme"] 是 stylekit 合并过 film.json 的 visual.theme_overrides 之后的最终主题，
    # resolve_style 的对比度体检（theme_lint）用的就是它。这里必须用同一份：否则
    # scenes 画背景吃 overrides、字幕底板/字色仍用基础主题——字幕压在同色背景上会看不清，
    # 而体检还照报通过（"门禁看着干净、其实没在查"）。老 stylekit / 老计划没有 theme
    # 字段时回落到基础主题，行为与本行改动前一致。
    th = plan.get("theme")
    if isinstance(th, dict):
        return th
    # **先看 plan["theme"]**（designkit 生成的计划自带完整 35 字段主题）。
    # 不能按 theme_id 去查表：没有内置主题表，设计路径的 theme_id（如 "a-44957fb5"）
    # 查不到 -> KeyError，整条出片链路会在 layout 阶段崩掉。
    th = plan.get("theme")
    if isinstance(th, dict) and th:
        return th
    # 没有 theme 计划就是"这份 script 没走过 design/propose"。
    # 明确报错并给迁移指引，不静默回落（静默回落 = 用一套不存在的默认外观出片）。
    raise SystemExit(
        "[style] 这份剧本没有可用的主题：\n"
        "  计划里缺 theme 字段，而内置主题表已在本版删除。\n"
        "  正确做法：python propose.py --brief \"题材\" --script film.json   # 出方案\n"
        "            python propose.py --confirm --candidate A               # 冻结 design.json\n"
        "            python render.py --script film.json --stage all\n"
        "  （theme_id=%r）" % (plan.get("theme_id"),))


def _print_style(plan):
    m = plan["music"]
    v = plan["voice"]
    print("   风格: %s（%s）%s" % (plan["style"], plan["style_source"],
                                  "" if plan["legacy"] else " / " + plan["style_name"]))
    print("        理由: %s" % "；".join(plan["style_reason"]))
    print("        主题: %s  版式: %s" % (plan["theme_id"],
                                         ",".join(sorted(set(plan["chapter_layout"].values())))
                                         or plan["layout_default"]))
    # 视觉语言（结构签名）也要能在这里看到 —— 用户判断"这套风格是不是只是换色"
    # 的第一现场就是 --stage style 的打印，藏起来等于让他去读 style.json。
    vl = plan.get("language") if isinstance(plan.get("language"), dict) else None
    if vl:
        mf = vl.get("motif") or {}
        tr = vl.get("transition") or {}
        ty = vl.get("typography") or {}
        bi = vl.get("bias") or {}
        print("        视觉语言: 母题=%s 转场=%s(%.2fs) 字距=%.1f 编号=%s 重心=(%+.0f,%+.0f)"
              % (mf.get("kind"), tr.get("kind"), float(tr.get("dur") or 0.0),
                 float(ty.get("tracking") or 0.0), ty.get("number"),
                 float(bi.get("x") or 0.0), float(bi.get("y") or 0.0)))
    # 不能无条件读 m["style"]：designkit 明确不写这个键（配乐风格表已删），
    # 那样公开命令 `--stage style` 会 100% KeyError: "style"。
    # 按「参数化配乐」的真实身份打印：调式 + BPM + 调性 + mood。
    _music_id = m.get("arranger") or m.get("style") or ("%s %gBPM" % (m.get("scale", "?"), m.get("bpm", 0)))
    print("        配乐: %s  BPM %g（%s）  调性 %s（%s）  mood %s"
          % (_music_id, m["bpm"], m["bpm_source"], m["key"], m["key_source"], m["mood"]))
    _sub = plan.get("subtitle") if isinstance(plan.get("subtitle"), dict) else None
    if _sub:
        _y = _sub.get("y")
        print("        字幕: %s %dpx 对齐=%s 背板α=%d 圆角=%d 最多%d行"
              % ("y=%s" % _y if _y is not None else "底部安全区",
                 int(_sub.get("size") or 46), _sub.get("align") or "center",
                 int(_sub.get("plate_alpha") if _sub.get("plate_alpha") is not None else 170),
                 int(_sub.get("radius") if _sub.get("radius") is not None else 10),
                 int(_sub.get("max_lines") or 2)))
    _cyc = plan.get("layout_cycle") or []
    if len(_cyc) >= 2:
        print("        版式轮换: %s（按章节轮换，防全片一张版式）" % " -> ".join(map(str, _cyc)))
    if v["map"]:
        print("        旁白: %s" % "  ".join("%s=%s" % kv for kv in sorted(v["map"].items())))
    p = plan["pacing"]
    print("        节奏: lead %.2f / tail %.2f / min_dur %.1f / 条目步长 %.2f"
          % (p["lead"], p["tail"], p["min_dur"], p["item_stagger"]))


def st_style(script, work):
    print("== Style")
    plan = resolve_style(script, work)
    _print_style(plan)
    return plan


# ---------------------------------------------------------------- TTS
def _voice_plan(script, plan):
    """声线计划（voicekit）。缺 voicekit 时返回 None -> tts_batch 走单声线路径。"""
    try:
        import voicekit as VK
    except Exception as e:                                  # noqa: BLE001
        print("   [WARN] 没有 voicekit.py，本次旁白单声线（单声线兼容）：%s" % e)
        return None
    try:
        vp = VK.voice_plan(script, plan)
    except Exception as e:                                  # noqa: BLE001
        print("   [WARN] voicekit 解析失败，退回单声线：%s" % e)
        return None
    return vp


def st_tts(script, work, force=False):
    lines = script["lines"]
    plan = resolve_style(script, work)
    print("== TTS (%d 句)" % len(lines))
    if plan["legacy"]:
        print("   声线: 兼容模式（单一声线 %s）" % script.get("voice", "zh-CN-YunxiNeural"))
        vp = None
    else:
        vp = _voice_plan(script, plan)
        if vp:
            import voicekit as VK
            print(VK.describe(vp))
            json.dump({"style": plan["style"], "policy": plan["voice"]["policy"], "lines": vp},
                      open(os.path.join(work, "voice_plan.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    if force:
        shutil.rmtree(os.path.join(work, "tts"), ignore_errors=True)
    want_words = bool(script.get("words", True))
    done = K.tts_batch(lines, work,
                       voice=script.get("voice", "zh-CN-YunxiNeural"),
                       rate=script.get("rate", "+0%"),
                       pitch=script.get("pitch", "+0Hz"),
                       workers=int(script.get("tts_workers", 4)),
                       trim=bool(script.get("trim_silence", True)),
                       plan=None if vp is None else
                       [{"voice": r["voice"], "rate": r["rate"], "pitch": r["pitch"],
                         "volume": r.get("volume", "+0%"),
                         "persona": r.get("persona"), "tone": r.get("tone")} for r in vp],
                       trim_db=float(script.get("trim_db", -42.0)),
                       words=want_words,
                       backend=str((script.get("tts") or {}).get("backend") or "edge"))
    json.dump(done, open(os.path.join(work, "tts.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    total = sum(float(x["dur"]) for x in done)
    print("   语音总时长 %.2fs / %d 句" % (total, len(done)))
    _write_words(work, done, want_words)
    return done


def _write_words(work, done, want_words):
    """把逐句词级时间戳汇总成 work/words.json。

    结构：{schema, enabled, lines:{<id>:{dur, n, covered, tail, mono, words:[...]}}, stats}
    门禁与字幕都从这里读，不再各自去翻 *.mp3.json（单一事实来源）。
    """
    if not want_words:
        print("   词级时间戳: 未启用（film.json 里 words=false）")
        return None
    lines = {}
    n_words = 0
    n_bad = []
    for row in done or []:
        ws = row.get("words") or []
        summ = K.words_summary(ws, row.get("dur"))
        lines[str(row["id"])] = dict(dur=round(float(row.get("dur") or 0.0), 4),
                                     words=ws, **{k: v for k, v in summ.items() if k != "n"},
                                     n=summ["n"])
        n_words += len(ws)
        if not ws:
            n_bad.append(row["id"])
    payload = dict(schema=1, enabled=True, lines=lines,
                   stats=dict(lines=len(lines), words=n_words, empty=n_bad))
    p = os.path.join(work, "words.json")
    json.dump(payload, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if n_bad:
        print("   [WARN] %d 句拿不到词级事件（%s）；这些句的词级门禁会记 skipped"
              % (len(n_bad), "、".join(n_bad[:6])))
    else:
        print("   词级时间戳: %d 句 / %d 词 -> %s" % (len(lines), n_words, p))
    return payload


# ---------------------------------------------------------------- 排版
def st_layout(script, work, lines=None):
    lines = lines or json.load(open(os.path.join(work, "tts.json"), encoding="utf-8"))
    plan = resolve_style(script, work)
    print("== Layout")
    pace = plan["pacing"]
    # 逐句 lead/tail：voicekit 的 voice_plan 已经算好每句的停顿，这里只负责喂给时间轴。
    # legacy 计划没有 voice_plan.json，lead_map/tail_map 都是 None -> 全局常数。
    vp = None
    vp_path = os.path.join(work, "voice_plan.json")
    if not plan["legacy"] and os.path.exists(vp_path):
        try:
            vp = json.load(open(vp_path, encoding="utf-8"))["lines"]
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] voice_plan.json 读不了，逐句停顿退回全局值：%s" % e)
    lead_map = {r["id"]: float(r["lead"]) for r in vp} if vp else None
    tail_map = {r["id"]: float(r["tail"]) for r in vp} if vp else None
    if vp and script.get("tail_map"):
        tail_extra = script["tail_map"]
        for lid, v in tail_extra.items():
            if lid in tail_map:
                tail_map[lid] = float(v)
    vp = K.video_plan(script)          # 画幅/帧率：非法配置在这里就报错，不拖到渲帧
    tl = K.build_timeline(lines,
                          lead=script.get("lead", pace["lead"]),
                          tail=script.get("tail", pace["tail"]),
                          min_dur=script.get("min_dur", pace["min_dur"]),
                          tail_map=tail_map or script.get("tail_map"),
                          lead_map=lead_map,
                          hold=script.get("hold", pace["hold"]),
                          max_dur=script.get("max_dur"),
                          fps=vp["fps"])
    tl["video"] = vp
    print("   画幅: %d×%d @ %d fps（内部构图 %d×%d%s）"
          % (vp["out_w"], vp["out_h"], vp["fps"], vp["w"], vp["h"],
             "，编码时 lanczos 缩放" if vp["scale"] else ""))
    tl["title"] = script["title"]
    tl["voice"] = script.get("voice", "zh-CN-YunxiNeural")
    # 词级时间戳（句子时长仍是权威；词只用于字幕断行/锚点/门禁）
    tl["words"] = bool(script.get("words", True))
    wp = os.path.join(work, "words.json")
    if tl["words"] and os.path.exists(wp):
        try:
            wj = json.load(open(wp, encoding="utf-8"))
            tl["word_stats"] = wj.get("stats")
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] words.json 读不了，词级能力本次不可用：%s" % e)
            tl["words"] = False
    tl["crf"] = script.get("crf", 18)
    tl["preset"] = script.get("preset", "medium")
    tl["style"] = {"style": plan["style"], "theme_id": plan["theme_id"],
                   "legacy": plan["legacy"],
                   "chapter_layout": plan["chapter_layout"],
                   "item_stagger": pace["item_stagger"]}
    # ---- 场景语法：内容 -> 构图选型（落 timeline.scenes[] 与 work/storyboard.json） ----
    # legacy 不注入：兼容绘制器只读 kind，注入新字段等于给基线加噪声。
    if not plan["legacy"]:
        try:
            import grammar as G
        except Exception as e:                              # noqa: BLE001
            G = None
            print("   [WARN] 没有 grammar.py，本次全部按 kind 走兼容构图：%s" % e)
        if G is not None:
            sb = G.plan_scenes(tl["scenes"], script, plan)
            try:
                json.dump(sb, open(os.path.join(work, "storyboard.json"), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
            except Exception as e:                          # noqa: BLE001
                print("   [WARN] storyboard.json 落盘失败：%s" % e)
            print("   分镜: %s" % G.describe(sb))
            for r in sb["scenes"]:
                print("     %-6s %-8s -> %-9s v%d d%d  %s"
                      % (r["id"], r["kind"], r["type"], r["variant"], r["decor"],
                         "；".join(r["reason"][:2]) or "-"))
            tl["storyboard"] = {"type_counts": sb["type_counts"]}
    # ---- 竖屏逐类停留：必须在 plan_scenes 之后（要读 scene_type）、落盘之前 ----
    tl = _apply_type_hold(tl, script, plan)
    json.dump(tl, open(os.path.join(work, "timeline.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n_srt = K.write_srt(tl["scenes"], os.path.join(work, "sub.srt"))
    # 同上：设计计划自带 theme，不能假定 theme_id 一定在内置表里（见 _theme_for 的注释）
    # 没有内置主题表：theme 必须由计划自带（design 路径一定有）
    theme = (plan.get("theme") if not plan["legacy"] else None)
    n_ass = K.write_ass(tl["scenes"], os.path.join(work, "sub.ass"), theme=theme)
    print("   总时长 %.2fs / %d 帧 / %d 段 / srt %d 条 / ass %d 条"
          % (tl["total"], tl["frames"], len(tl["scenes"]), n_srt, n_ass))
    # 安全区自查：字幕一旦越界，mux 前就能发现
    safe_errs = K.qa_text_safe(tl["scenes"])
    if safe_errs:
        print("   [WARN] 字幕安全区:", safe_errs[:3])
    # 字形自查：缺字符会被画成方块
    glyph_errs = K.check_glyphs(script["lines"] + tl["scenes"])
    if glyph_errs:
        print("   [WARN] 缺字形（会显示为方块）:")
        for lid, fld, chars in glyph_errs[:8]:
            print("     %-6s %-7s %r" % (lid, fld, chars))
    for s in tl["scenes"]:
        print("   %-8s %7.2f-%7.2f  %s" % (s["id"], s["start"], s["end"], s["text"][:40]))
    # ---- 目标时长对账 ----
    # brief 里写的「2 分钟 / 60 秒」只影响 design.structure.segments 与 target_sec，
    # 而成片时长由 TTS 实测决定 —— 用户以为"我要求了时长"，实际拿到 23s。
    # 不改时长（截断旁白或拉长留白都是坏做法），但**必须当场说出来差多少**，
    # 并给出可执行的做法（加台词 / 调 hold / 减台词）。
    target = (plan or {}).get("target_sec")
    if target:
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = None
    # 语义分两档（见 design.structure.target_mode）：
    #   exact    = 精确目标，双向都告警（用户写「2 分钟」的常规理解）；
    #   at_least = 下限，只有**不足**才告警——超出去是好事，不该报红。
    # 两档都不截断旁白：成片长度永远由 TTS 实测决定。
    mode = str((plan or {}).get("target_mode") or "exact")
    if target and target > 0:
        diff = float(tl["total"]) - target
        pct = diff / target * 100.0
        label = "下限" if mode == "at_least" else "目标"
        print("   %s时长 %.1fs / 实际 %.2fs（%+.1fs，%+.0f%%）"
              % (label, target, tl["total"], diff, pct))
        warn = (pct < -15.0) if mode == "at_least" else (abs(pct) > 15.0)
        if warn:
            head = ("没有达到下限（差 %.1fs）：成片时长由**实测旁白**决定，" % (-diff)
                    if mode == "at_least" else
                    "与目标时长差超过 15%：成片时长由**实测旁白**决定，不收口就要改文案。")
            print("   [WARN] " + head
                  + "\n          想变长：加台词或调大 film.json 的 hold / lead / tail；"
                  + "\n          想变短：删减台词（hold 只会加留白，不会缩短旁白）。")
    return tl


# ---------------------------------------------------------------- 竖屏逐类停留
# 为什么按构图类型再分配停留：chart 段要看清 6 根条与标签，hero 段只要认出标题，
# 统一 pace 只能兼顾平均，结果必然是"数据段没看清、金句段干等"。
# 只作用于竖屏：横屏恒不加（既有横屏片的时长与 golden_frames 都不许变）。
_PORTRAIT_TYPE_HOLD = {
    "chart": 0.45, "list": 0.30, "compare": 0.40, "process": 0.40,
    "stat": 0.25, "chapter": 0.15, "outro": 0.15,
    "hero": 0.0, "statement": 0.0, "quote": 0.0,
}


def _shift_scene(scene, delta):
    """把一个 scene 上所有**绝对时间**字段整体平移 delta（秒）。"""
    if not delta:
        return
    for key in ("start", "end", "voice_start", "voice_end"):
        v = scene.get(key)
        if v is not None:
            scene[key] = round(float(v) + delta, 4)
    was = scene.get("words_abs")
    if isinstance(was, list) and was:
        out = []
        for w in was:
            if not isinstance(w, dict):
                out.append(w)
                continue
            nw = dict(w)
            for key in ("start", "end"):
                if nw.get(key) is not None:
                    nw[key] = round(float(nw[key]) + delta, 4)
            out.append(nw)
        scene["words_abs"] = out


def _apply_type_hold(tl, script, plan):
    """竖屏：按 scene_type 追加段尾停留，并把后续段整体后移。横屏原样返回。

    为什么不是"给每段单独改 end 就完事"：段是首尾相接的时间轴，第 i 段变长后，
    第 i+1 段起的**所有**绝对时间（start/end/voice_start/voice_end/words_abs）
    都得跟着后移，否则段会互相重叠、旁白与画面直接错位。
    同时更新 total / frames —— 它们是 mux 与 QA 的时长基准。

    film.json 显式写了 hold 时**不加码**：用户定的停留就是他的意思，不该被悄悄改大。
    """
    from layoutkit import W as _W, H as _H        # noqa: PLC0415 —— 只读画布判断
    if int(_H) <= int(_W):
        return tl                                  # 横屏：零改动
    # 用户显式写的 hold 与逐类停留**叠加**：两者解决的不是同一件事——
    # hold 是「所有段都多停一会」，逐类停留是「数据与对照多停、金句不用」。
    # 叠加后总增量有上限（_MAX_TYPE_HOLD_EXTRA），避免把片子拖成等得着急。
    _ = script.get("hold")          # 记一笔：这里只是不再因它早退
    scenes = tl.get("scenes") or []
    if not scenes:
        return tl
    shift = 0.0
    applied = {}
    for s in scenes:
        if shift:
            _shift_scene(s, shift)
        stype = str(s.get("scene_type") or "")
        extra = float(_PORTRAIT_TYPE_HOLD.get(stype) or 0.0)
        if extra > 0:
            s["end"] = round(float(s["end"]) + extra, 4)
            s["hold_type"] = round(extra, 3)
            applied[s["id"]] = (stype, round(extra, 3))
            shift += extra
    if not applied:
        return tl
    # 上限：逐类停留总共不超过片长的 6%（也至少 3 秒），防止长片被拖成慢节奏。
    # 为什么给上限：逐类停留是按段累加的，55 段的片子会自动涨到十几秒，
    # 那时该改的是台词节奏，不是继续加停顿。
    _base = float(tl.get("total") or 0.0)
    if _base > 0:
        _cap = max(3.0, _base * 0.06)
        if shift > _cap:
            _k = _cap / shift
            shift = 0.0
            for s in scenes:
                h = float(s.get("hold_type") or 0.0)
                if h > 0:
                    s["hold_type"] = round(h * _k, 3)
                    s["end"] = round(float(s["end"]) - h + h * _k, 4)
            shift = round(sum(float(s.get("hold_type") or 0.0) for s in scenes), 3)
            # 重排一次，保持首尾相接
            _t = 0.0
            for s in scenes:
                _dur = float(s["end"]) - float(s["start"])
                s["start"] = round(_t, 4)
                s["end"] = round(_t + _dur, 4)
                _t = float(s["end"])
            tl["total"] = round(_t, 4)
    tl["total"] = round(float(scenes[-1]["end"]), 4)
    fps = int(tl.get("fps") or 30)
    tl["frames"] = int(round(tl["total"] * fps))
    tl["type_hold"] = {"applied": len(applied), "extra_s": round(shift, 2),
                       "by_id": {k: v[1] for k, v in applied.items()}}
    print("   竖屏逐类停留: %d 段 +%.2fs（%s）"
          % (len(applied), shift,
             "/".join(sorted({v[0] for v in applied.values()}))))
    return tl


# ---------------------------------------------------------------- 提示点
def st_cues(script, work, tl, engine="pil"):
    """画面提示点 -> work/cues.json。

    三个来源按顺序合并（后写的覆盖同类型同时间的项不做去重，音效叠加是合理行为）：
      1. film.json 的 sfx.cues（与引擎无关，手写最直观）
      2. scenes.py 的可选钩子 cues(tl, script)（代码里登记：画面算到哪就响到哪）
      3. web 引擎：页面的 window.getCues()（提示点跟着动画代码走，改动画不用改 JSON）
    """
    print("== Cues")
    bus = K.CueBus(tl["scenes"], tl["total"])
    sfx_cfg = script.get("sfx") or {}
    skipped = []

    def _extend_safe(items, src):
        """逐条登记；某条写错了（场景 id 不存在）只跳过它并记下来，不炸掉整个 stage。

        全部跳过时报错——"提示点一个都没落上"和"少了两个提示点"是两回事，
        前者说明剧本/分段对不上，不能静默出无声片。
        """
        n = 0
        for it in items or []:
            try:
                bus.extend([it])
                n += 1
            except Exception as e:                          # noqa: BLE001
                skipped.append((src, it, str(e)))
        return n

    n_json = 0
    if sfx_cfg.get("enabled", True):
        n_json = _extend_safe(sfx_cfg.get("cues"), "film.json")

    # scenes.py 里的 cues() 钩子：允许直接返回 [{at,type,...}] 或调用 bus.add(...)
    n_hook = 0
    mod = None
    # scenes.py 的 cues() 常要读主题/视觉语言（assets 版就调 resolve_script_style）。
    # 单跑 `--stage cues` 时前面没有 stage 把 script["_style"] 挂好，钩子就会去解析"设计"
    # 并直接 raise —— 公开命令 100% 崩，报错还指向"先去提案"（其实设计已确认）。
    # 所以在调用钩子前解析一次（幂等，已挂 _style 时直接复用）。
    if script.get("_scenes") and os.path.exists(script["_scenes"]) and not isinstance(script.get("_style"), dict):
        try:
            script["_style"] = resolve_style(script, work)
        except SystemExit:
            pass                      # 没设计：cues 阶段不因它失败，钩子里真需要会自己报错
    if script.get("_scenes") and os.path.exists(script["_scenes"]):
        try:
            mod = load_module(script["_scenes"])
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] 加载 scenes.py 失败，跳过 cues 钩子:", e)
            mod = None
    if mod is not None and hasattr(mod, "cues"):
        try:
            got = mod.cues(tl, script) or []
        except Exception as e:                              # noqa: BLE001
            raise SystemExit("scenes.py 的 cues() 钩子报错：%s" % e) from e
        n_hook = _extend_safe(got, "scenes.py")
        # web 引擎下要提醒"两套时间基准混用"。
        # scenes.py 的 cues() 是按 **PIL 时间轴**（timeline.json 的段落与 visual_at）算的，
        # 页面动画则按 **页面自己的时间轴**（window.DURATION）。两者合并进同一份 cues.json 后：
        #   - 音效会按 PIL 时间响（页面画面此刻可能正在另一幕）；
        #   - 音画同步门禁能把这种配对识别为"跨基准"并跳过（qa_frames 的 src 过滤），不报假 FAIL，
        #     但**声音与画面对不上的事实仍然存在**。
        # 不静默：说清现象与两条改法，让用户自己选路线。
        if engine == "web" and n_hook:
            print("   [WARN] web 引擎下仍合并了 scenes.py 的 %d 条 cue —— 它们按 PIL 时间轴生成，"
                  "页面动画按页面时间轴播放，两者基准不同：音效可能响在错误的幕上。" % n_hook)
            print("          两种改法：① 走 PIL 路线（--engine pil）用 scenes.py 这套 cue；"
                  "② 走 web 路线就把 cue 写进页面（window.getCues()）或 film.json 的 sfx.cues，"
                  "并删除 scenes.py 的 cues() 钩子。")

    # web 引擎：从页面导出（需要浏览器）
    n_web = 0
    events = []
    if engine == "web":
        from_web = _st_cues_web(script, work)
        for it in from_web.get("cues") or []:
            row = dict(it)
            row["src"] = "web"
            bus.items.append(row)
            n_web += 1
        events = from_web.get("events") or []
        print("   页面导出 %d 个提示点 / %d 个视觉事件" % (n_web, len(events)))

    if skipped:
        print("   [WARN] %d 条提示点被跳过（场景 id 对不上，通常是改了剧本没同步 film.json）：" % len(skipped))
        for src, it, err in skipped[:6]:
            print("     %-10s %s  ->  %s" % (src, it, err.split("（")[0][:90]))
        if not bus.items and (sfx_cfg.get("cues") or n_hook):
            raise SystemExit("[cues] 所有提示点都落位失败：上面的 id 一个都没在 timeline 里。"
                             "先跑 --stage layout，再核对 film.json 的 sfx.cues 用的是当前场景 id。")

    payload = bus.write(os.path.join(work, "cues.json"), duration=tl["total"], events=events)
    print("   cues.json: %d 条（film.json %d / scenes.py %d / 页面 %d）"
          % (len(payload["cues"]), n_json, n_hook, n_web))
    kinds = {}
    for c in payload["cues"]:
        kinds[c["type"]] = kinds.get(c["type"], 0) + 1
    if kinds:
        print("   类型:", ", ".join("%s×%d" % (k, v) for k, v in sorted(kinds.items())))
    return payload


def _st_cues_web(script, work):
    """调用 webengine.export_cues 导出页面提示点（engine=web 时）。"""
    we = _opt_module("webengine", "浏览器逐帧渲染需要 webengine.py；只用 PIL 路线可以不改 engine")
    entry = _web_entry(script)
    tl = {}
    try:
        tp = os.path.join(str(work), "timeline.json")
        if os.path.exists(tp):
            tl = json.load(open(tp, encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        tl = {}
    style = _web_style_payload(we, script, tl, work)
    vp = (tl.get("video") or {})
    data = we.export_cues(entry, style=style,
                          width=int(vp.get("out_w") or K.W),
                          height=int(vp.get("out_h") or K.H),
                          fps=int(tl.get("fps") or K.FPS))
    return data or {}


# ---------------------------------------------------------------- 配乐 / 音效
def _score_with_musicgen(script, work, tl, plan_s, mus):
    """用参数化编排器出成片配乐（与 previewkit 的样音同一个引擎、同一组参数）。

    为什么必须走参数化编排：本技能不提供风格表/模板，配乐必须由设计里的参数现场生成。
    musicgen 的六个参数（progressions / motif / drums / voices / mood / sections）
    全部参与事件展开，实测参数相关性 seed 0.16 / mood 0.32 / voices 0.22，无一是死旋钮。
    """
    print("== Score（musicgen 参数化编排）")
    mg = _try_module("musicgen")
    mp = plan_s["music"]
    sections = mus.get("sections")
    if sections:
        fixed = []
        for s in sections:
            s2 = dict(s)
            if "at" in s2:
                s2["start"] = K.cue_time(s2.pop("at"), tl["scenes"])
            fixed.append(s2)
        sections = fixed
    kwargs = dict(
        bpm=float(mp["bpm"]),
        key=str(mp["key"] or "Dm"),
        scale=str(mp.get("scale") or "minor"),
        mood=str(mp.get("mood") or "steady"),
        # seed_effective 是"内容指纹掺进去之后"的 32 位大数；musicgen 内部按
        # sha1(seed) 派生，直接给大整数没问题，但统一夹到 2^31-1 内可以保证
        # 取模口径固定（既有 work/ 目录里的 score.json 能对上）。
        seed=int(mp.get("seed_effective") or mp.get("seed") or 0) % (2 ** 31 - 1),
        sections=sections,
    )
    # 设计里显式给出的编排参数原样透传（progressions/motif/drums/voices/energy_curve）。
    # 只在键存在时传：musicgen 对 None 与缺省的处理不同（缺省=自己按 seed 派生）。
    for key in ("progressions", "motif", "drums", "voices", "energy_curve"):
        if mp.get(key) is not None:
            kwargs[key] = mp[key]
    t0 = time.time()
    try:
        samples, report = mg.render(float(tl["total"]), **kwargs)
    except Exception as e:                                  # noqa: BLE001
        raise SystemExit(
            "[score] musicgen.render() 失败：%s\n"
            "  参数：bpm=%s key=%s scale=%s mood=%s seed=%s\n"
            "  这是配乐生成的真错误（不是签名不兼容）——先把参数改对，别改成静默跳过。"
            % (e, kwargs.get("bpm"), kwargs.get("key"), kwargs.get("scale"),
               kwargs.get("mood"), kwargs.get("seed"))) from e
    out = os.path.join(work, "music.wav")
    mg.save(out, samples)
    if report:
        json.dump(report, open(os.path.join(work, "score.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    print("   musicgen: %s %s / %s BPM / %s / mood %s / seed %s"
          % (report.get("key_name") or report.get("key"), report.get("scale"),
             report.get("bpm"), "", report.get("mood"), report.get("seed")))
    samples_read, _sr = K.wav_read(out)
    print("   music.wav %.2fs  峰值 %.3f  %.1fs" % (
        K.wav_dur(out), float(np.max(np.abs(samples_read))), time.time() - t0))
    return out


def st_score(script, work, tl):
    """参数化配乐 -> work/music.wav（musicgen 现场编排，没有模板可回退）。

    配乐来源由 K.music_plan 统一判定（与 st_audio 共用同一份语义）：
      - mode=off/none 或 enabled=false：不出配乐，也不删已存在的 music.wav
        （删文件会误删"外部提供的配乐"，而 off 的语义只是"本片不要配乐"）；
      - mode=bed：走和弦床，不生成 musicgen 产物；
      - mode=file：外部 work/music.wav，原样交给 audio 阶段；
      - 缺省：有 bpm 就生成，没有 bpm 沿用已有 music.wav / 和弦床。

    需要真的生成配乐时，**只有 musicgen 一条路**：legacy 计划或 musicgen.py 缺失都会
    明确报错并给迁移指引，不用 scorekit 的配乐模板兜底（零模板）。
    """
    print("== Score")
    mus = script.get("music") or {}
    music_path = os.path.join(work, "music.wav")
    plan, why = K.music_plan(script, has_music_file=os.path.exists(music_path))
    print("   " + why)
    if plan in ("off", "bed", "file", "none"):
        return None
    if plan in ("missing-file", "missing-bpm", "unknown-mode"):
        raise SystemExit("[score] " + why)
    # **配乐只有 musicgen 一条路**（参数化编排器，没有风格表）。
    #
    # 为什么必须同源：若 previewkit 用 musicgen 出 12 秒样音、而 st_score 用 scorekit
    # 出成片音乐，两者是**两个不同的作曲器**，实测同一设计下相关系数 -0.0057（就是两首
    # 不同的曲子）。用户看样音选方案，成片音乐完全不同，等于"确认"这个动作在音频上失效。
    #
    # scorekit 没有风格表（本技能不提供模板），它的 render() 只剩 legacy 逐样本兼容路径。
    # 所以这里**没有 scorekit 回退**：scorekit 一旦回来当兜底，成片音乐就又变成一套
    # 固定模板，与"每条视频都不一样"直接冲突。
    plan_s0 = resolve_style(script, work)
    if plan_s0.get("legacy"):
        raise SystemExit(
            "[score] legacy 配乐路径已移除（零模板）：\n"
            "  scorekit 的 9 套配乐风格表已删除，legacy 回退会把成片配乐锁死成一套固定模板。\n"
            "  正确做法（两步）：\n"
            "    1) python propose.py --brief \"题材/要求\" --script film.json   # 出 3 个方案\n"
            "    2) python propose.py --confirm --candidate A                   # 冻结 work/design.json\n"
            "    3) python render.py --script film.json --stage score           # 用 musicgen 现场编排\n"
            "  （想手写配乐参数：film.json 的 music.bpm / key / mood / sections 仍然有效）")
    if _try_module("musicgen") is None:
        raise SystemExit(
            "[score] 找不到 musicgen.py，无法生成配乐：\n"
            "  配乐只有参数化编排器一条路（scorekit 的风格模板已删除，不再兜底）。\n"
            "  检查 <SKILL_DIR>/scripts/musicgen.py 是否存在（技能脚本就在技能目录里跑，不用拷）；\n"
            "  只需要旁白/音效时把 film.json 的 music.mode 设为 off 或 file。")
    return _score_with_musicgen(script, work, tl, plan_s0, mus)


def _sfx_seed_context(script, work=None):
    """算音效的内容派生上下文 (style, content_fp, key, enabled)。

    legacy（没写 style 字段）时返回全 None -> CueBus 走 seed_base + i 路径，
    逐样本与基线一致。参考 references/audio-variation.md §3.2 / §4。
    """
    plan = resolve_style(script, work)
    sfx_cfg = script.get("sfx") or {}
    want_content = bool(sfx_cfg.get("content_seed", True))
    if plan.get("legacy") or not want_content:
        return None, None, None
    fp = (plan.get("fingerprint") or {}).get("full")
    mus = plan.get("music") or {}
    return str(plan.get("style")), str(fp) if fp else None, str(mus.get("key") or "Dm")


def _duck_transition_sfx(samples, cues, scenes, sr=K.SR, depth_db=-3.0, window=2.2):
    """段落边界前的转场音效自动让位（契约 §4）。

    配乐在段落交界自己已经有一段过门（编排器写的 swell / riser / sub_boom），
    sfx 轨再在同一时刻放 whoosh / riser，同一频段叠两层就是"配乐和音效听起来像同一套"。
    做法：对位于 [边界-window, 边界] 内的转场类 cue，在它的时长内乘一个 dB 增益包络。
    只压转场类，不碰 impact / 提示音（那是"该响"的语义重音）。
    """
    if not scenes or samples is None or not len(samples):
        return samples
    try:
        import numpy as np
    except Exception:                                       # noqa: BLE001
        return samples
    x = np.array(samples, copy=True, dtype=np.float32)
    n = len(x)
    bounds = [float(s["start"]) for s in scenes if float(s.get("start", 0.0)) > 0.5]
    if not bounds:
        return x
    kinds = 0
    for c in cues or []:
        if str(c.get("type")) not in ("whoosh", "riser", "reverse_swell"):
            continue
        t = float(c.get("t", -1.0))
        if t < 0:
            continue
        near = any(abs(t - b) <= window for b in bounds)
        if not near:
            continue
        dur = float(c.get("dur") or 1.5)
        s = max(0, int(round(t * sr)))
        e = min(n, int(round((t + dur) * sr)))
        if e <= s:
            continue
        g = 10.0 ** (depth_db / 20.0)
        k = min(80, max(8, int(0.01 * sr)))
        ramp = min((e - s) // 2, k)
        seg = x[s:e]
        if ramp > 0:
            env = np.full(e - s, g, dtype=np.float32)
            env[:ramp] = np.linspace(1.0, g, ramp, dtype=np.float32)
            env[-ramp:] = np.linspace(g, 1.0, ramp, dtype=np.float32)
            seg = seg * env[:, None] if seg.ndim == 2 else seg * env
        else:
            seg = seg * g
        x[s:e] = seg
        kinds += 1
    if kinds:
        print("   过门避让: %d 条转场音效在段落边界前 %.1fs 内 -%.0f dB（避免和配乐过门撞车）"
              % (kinds, window, abs(depth_db)))
    return x


def st_sfx(script, work, tl):
    """按提示点合成音效轨 -> work/sfx.wav。没有 cues.json 就跳过。

    非 legacy 时走内容派生 seed（跨片同一位置不逐样本相同），
    并把配乐调性透传给提示音配方（不硬编码 Dm）；
    段落边界前的转场音效自动让位（不与配乐过门撞车）。
    """
    print("== SFX")
    cues_path = os.path.join(work, "cues.json")
    if not os.path.exists(cues_path):
        print("   没有 cues.json（先跑 --stage cues），本次不出音效轨")
        return None
    bus = K.CueBus.from_json(cues_path)
    bus.total = float(tl["total"])
    if not bus.items:
        print("   cues.json 里没有提示点，跳过")
        return None
    sfx_cfg = script.get("sfx") or {}
    style, content_fp, key = _sfx_seed_context(script, work)
    try:
        samples = bus.build_audio(total=tl["total"],
                                  base_db=sfx_cfg.get("base_db"),
                                  default_gain_db=float(sfx_cfg.get("gain_db", 0.0)),
                                  style=style, content_fp=content_fp, key=key)
    except Exception as e:                                  # noqa: BLE001
        # 音效轨是"增强"不是"必需"：sfxkit 缺失/配方不全时不要拖死整片，
        # 但必须把原因喊出来（静默降级比报错更危险）。
        print("   [WARN] 音效轨合成失败，本片只有一个旁白+配乐的混音：%s" % e)
        stale = os.path.join(work, "sfx.wav")
        if os.path.exists(stale):
            os.remove(stale)
        return None
    if not sfx_cfg.get("duck_transitions", True):
        pass
    else:
        samples = _duck_transition_sfx(samples, bus.resolved(), tl.get("scenes") or [])
    out = os.path.join(work, "sfx.wav")
    peak = K.wav_write(out, samples)
    print("   sfx.wav %.2fs  峰值 %.3f  声道 %d" % (
        K.wav_dur(out), peak, 2 if samples.ndim == 2 else 1))
    return out


def _voice_track(tl, script):
    """把逐句旁白铺成一条总线，返回 (n,) 单声道。

    全局增益只能在一处乘：voice_gain 若在这里和 mix_film 里**各乘一次**，
    净效果是 voice_gain^2（默认 1.0 时看不出来，改了才炸）。
    所以全局增益只在 mix_film 乘一次，这里只负责**逐句**增益
    （voicekit 的 gain_db，默认 0 dB -> 1.0）。
    """
    bus = K.AudioBus(tl["total"])
    for s in tl["scenes"]:
        pcm = K.decode_pcm(s["path"])
        gdb = float(s.get("gain_db", 0.0) or 0.0)
        bus.place(pcm, s["voice_start"], gain=10.0 ** (gdb / 20.0),
                  fade_in=0.02, fade_out=0.06)
    return bus.bus[: int(round(float(tl["total"]) * K.SR))].copy()


def _legacy_music(tl, script):
    """和弦床（没有 music.wav 时的兜底）。"""
    mus = script.get("music") or {}
    if not (mus.get("enabled", True) and mus.get("chords")):
        return None
    bus = K.AudioBus(tl["total"], channels=2)
    bus.music_bed(mus["chords"], seg=float(mus.get("seg", 4.0)),
                  gain=float(mus.get("gain", 0.9)),
                  amp=float(mus.get("amp", 0.055)),
                  xfade=float(mus.get("xfade", 1.2)))
    return bus.bus[: int(round(float(tl["total"]) * K.SR))].copy()


# ---------------------------------------------------------------- 混音
def st_audio(tl, work, script):
    """三总线混音：旁白 + 配乐 + 音效 -> audio.wav（立体声）。

    三条总线分开处理再合：
      - 旁白：单声道，居中（voice_gain 可调）
      - 配乐：来源由 K.music_plan 判定（score/file/bed/off），口径与 st_score 完全一致——
        music.mode=off 时一条配乐都不混，即使 work/music.wav 还在（外部配乐请用 mode=file）
      - 音效：work/sfx.wav（按 cues.json 合成）；没有 cues 时退回转场 whoosh
    音效响起时配乐自动让位（duck_db），避免重击被配乐吃掉。
    """
    print("== Audio")
    total = float(tl["total"])
    voice = _voice_track(tl, script)

    mus = script.get("music") or {}
    music = None
    music_path = os.path.join(work, "music.wav")
    plan, why = K.music_plan(script, has_music_file=os.path.exists(music_path))
    if plan == "off":
        # 关键语义：mode=off = 不要配乐。无条件读 work/music.wav 的话，
        # "上一轮跑出来的配乐"仍会被混进新片（用户改了 mode 却听不出变化）。
        print("   配乐: " + why)
    elif plan == "bed":
        music = _legacy_music(tl, script)
        if music is not None:
            print("   配乐: 和弦床（%s）" % why)
        else:
            print("   配乐: %s，但没有 music.chords，跳过" % why)
    elif plan in ("missing-file", "missing-bpm", "unknown-mode"):
        raise SystemExit("[audio] " + why)
    elif plan in ("file", "score"):
        music, _ = K.wav_read(music_path)
        print("   配乐: music.wav（%s）" % why)
    elif plan == "none":
        print("   配乐: " + why)
    if plan == "score" and music is None:
        # 单独跑 --stage audio 时 score 还没跑过：不混配乐，但必须说清楚为什么
        print("   [WARN] music.mode=score 但 %s 不存在（先跑 --stage score）；本次无配乐"
              % music_path)

    sfx = None
    sfx_path = os.path.join(work, "sfx.wav")
    if os.path.exists(sfx_path):
        sfx, _ = K.wav_read(sfx_path)
        print("   音效: sfx.wav（按 cues.json 合成）")
    elif script.get("whoosh", True) and not os.path.exists(os.path.join(work, "cues.json")):
        # 兼容路径：没有提示点时才用"每段开头一声 whoosh"的行为
        bus = K.AudioBus(total, channels=2)
        for s in tl["scenes"][1:]:
            if float(s["start"]) < 0.2:
                continue
            bus.place(K.whoosh(0.45, amp=float(script.get("whoosh_amp", 0.06))),
                      max(0.0, s["start"] - 0.12), gain=0.7)
        sfx = bus.bus[: int(round(total * K.SR))].copy()
        print("   音效: 旧版转场 whoosh（没有 cues.json）")

    plan_s = resolve_style(script, work)
    # 风格化的 duck 深度：轻快/极简风格的配乐更"薄"，让位可以浅一点；
    # 电影/史诗要压得更狠。用户显式写 music.duck_db 时永远以用户为准。
    # 整体 +1 dB 的来由：更低的值（2.0/2.5/3.0/4.5）下配乐只比音效低 1.5 dB、duck 仅 3 dB，
    # 段落过门时两者在同一频段几乎等高，是"配乐和音效听起来像同一套"的混音侧成因。
    duck_default = 4.0
    if not plan_s["legacy"]:
        duck_default = {"mono": 3.0, "lo-fi": 3.5, "magazine": 3.5,
                        "cinema": 5.5, "epic": 5.5}.get(plan_s["style"], 4.0)
    samples = K.mix_film(voice, music, sfx, total=total,
                         voice_gain=float(script.get("voice_gain", 1.0)),
                         # 旁白优先：配乐基线 -12 dB。
                         # 视频以旁白为主，-1.5 dB 时背景与台词响度几乎等高，观众要费力分辨字；
                         # 12 dB 是"人声明显压住背景"的经验线（audio.md §2 的那条设计标准）。
                         music_gain_db=float(mus.get("gain_db", -12.0)),
                         sfx_gain_db=float((script.get("sfx") or {}).get("mix_gain_db", 0.0)),
                         duck_db=float(mus.get("duck_db", duck_default)),
                         # 旁白说话时再压背景（侧链）：配乐 6 dB、音效 3 dB；
                         # 间隙自动恢复，避免整片背景被闷住。显式写 0 可关掉。
                         voice_duck_db=float(mus.get("voice_duck_db", 6.0)),
                         voice_duck_sfx_db=float(mus.get("voice_duck_sfx_db", 3.0)),
                         voice_thresh_db=float(mus.get("voice_thresh_db", -42.0)),
                         voice_floor_db=float(mus.get("voice_floor_db", -60.0)),
                         # SNR 兜底：混音后实测人声活跃区的人声/背景差，不足 12 dB 自动补压背景。
                         # 这是"画面/素材电平再离谱也不许盖住旁白"的最后一道闸；0 = 关。
                         guard_snr_db=float(mus.get("guard_snr_db", 12.0)),
                         guard_cap_db=float(mus.get("guard_cap_db", 30.0)))
    out = os.path.join(work, "audio.wav")
    peak = K.wav_write(out, samples)
    print("   audio.wav %.2fs  峰值 %.3f  声道 %d" % (K.wav_dur(out), peak, 2))
    return out


# ---------------------------------------------------------------- 预检闸门
def _run_preflight(script, work, engine, tl=None, skip=False):
    """渲染前资源闸门：缺字形 / 坏音频 / 缺字体，在渲第一帧之前就 fail。

    为什么值得单独立一道闸：这类问题若只在"片渲完、QA 才报"——870 帧白跑，
    而且 reports 里只有一句"缺字形"，说不清是哪个字段哪句台词。
    三档语义：没装 preflight.py -> skipped（写明原因）；有 error -> fail；
    只有 warning -> pass（warning 仍进账本，不静默）。
    """
    lg = _gate()
    pf = _try_module("preflight")
    if skip:
        if lg is not None:
            lg.bypass("渲染前资源预检（--no-preflight）", "本条不算通过，只记录")
        print("   [preflight] --no-preflight：跳过资源预检（QC 报告里会留痕）")
        return True
    if pf is None:
        if lg is not None:
            lg.skip("G0", "渲染前资源预检", "没有 preflight.py（老版本工作目录，拷一份进来才能查）")
        print("   [preflight] 没有 preflight.py，跳过资源预检（会记 skipped）")
        return True
    try:
        res = pf.check(script, work=work, engine=engine, timeline=tl)
    except Exception as e:                                  # noqa: BLE001
        if lg is not None:
            lg.fail("G0", "渲染前资源预检", "预检本身报错：%s" % e)
        print("   [FAIL][preflight] 预检自身出错：", e)
        return False
    for w in (res.get("warnings") or [])[:6]:
        print("   [WARN][preflight] %s" % (w.get("msg") if isinstance(w, dict) else w))
    errs = res.get("errors") or []
    st = (res.get("checks") or {})
    detail = " ".join("%s=%s" % (k, v.get("status")) for k, v in st.items()) or "-"
    if errs:
        if lg is not None:
            lg.fail("G0", "渲染前资源预检", "%d 处问题：%s" % (
                len(errs), "；".join((e.get("msg") if isinstance(e, dict) else str(e))
                                     for e in errs[:3])))
        print("   [FAIL][preflight] %d 处问题（下面是前几条）：" % len(errs))
        for e in errs[:8]:
            msg = e.get("msg") if isinstance(e, dict) else str(e)
            where = (e.get("where") if isinstance(e, dict) else "") or ""
            print("     - %s%s" % ((where + "：") if where else "", msg))
        return False
    if lg is not None:
        lg.ok("G0", "渲染前资源预检", detail)
    print("   [preflight] %s" % detail)
    return True


# ---------------------------------------------------------------- 字幕样式
_SUB_CACHE = {}


def _sub_kw(script):
    """从已解析的 plan 里取字幕参数（缓存按 script 对象身份，连续帧不重复解析）。

    字幕样式由 design.json 的 visual.subtitle 决定；参考帧渲染路径
    （qavmaf._draw_ref_frame）必须调用同一个 filmkit.subtitle_kwargs(plan)，
    否则两帧字幕参数不同会把同源好片的 VMAF 拉低。
    """
    key = id(script)
    cached = _SUB_CACHE.get(key)
    if cached is not None:
        return cached
    plan = script.get("_style") if isinstance(script.get("_style"), dict) else None
    kw = K.subtitle_kwargs(plan)
    if len(_SUB_CACHE) > 8:            # 批量渲染会跨很多 script：别无限长
        _SUB_CACHE.clear()
    _SUB_CACHE[key] = kw
    return kw


# ---------------------------------------------------------------- 渲染
def _render_range(tl, mod, script, f0, f1, out_path, crf, preset, progress=True):
    """渲染 [f0, f1) 帧区间到 out_path。纯函数：同一帧号永远画出同一张图。"""
    scenes = tl["scenes"]
    total = tl["total"]
    _vp = K.video_plan(script)
    sink = K.FrameSink(out_path, fps=float(tl["fps"]), crf=crf, preset=preset,
                       out_w=_vp["out_w"], out_h=_vp["out_h"])
    idx = 0
    t_start = time.time()
    last_rep = 0
    for fi in range(f0, f1):
        t = fi / float(tl["fps"])
        while idx + 1 < len(scenes) and t >= scenes[idx + 1]["start"]:
            idx += 1
        s = scenes[idx]
        lt = t - s["start"]
        frame = mod.draw_frame(s, lt, s["dur"], t, script, fi)
        if frame is None:
            raise RuntimeError("场景 %s 在 t=%.2fs 返回 None" % (s.get("id"), t))
        th = _theme_for(script) if _THEME_CACHE.get("k") != id(script) else _THEME_CACHE.get("th")
        if id(script) != _THEME_CACHE.get("k"):
            _THEME_CACHE.update(k=id(script), th=th)
        _hold = script.get("sub_max_hold")
        frame = K.draw_subtitle(frame, K.sub_at(scenes, t, _hold), theme=th,
                                words=K.sub_words_at(scenes, t, _hold),
                                **_sub_kw(script))
        fade = 1.0
        if t < 0.5:
            fade *= K.ease_out(t / 0.5)
        if t > total - 0.8:
            fade *= max(0.0, (total - t) / 0.8)
        if fade < 0.999:
            # 浅色主题淡入淡出到纯黑会出现"闪一下黑"，非常廉价。
            # transition=wash 时淡向主题底色，观感是"渐显"而不是"黑场"。
            wipe = (0, 0, 0)
            if th is not None and str(th.get("transition") or "black") == "wash":
                wipe = tuple(th["bg2"])
            frame = K.Image.blend(K.Image.new("RGB", (K.W, K.H), wipe), frame, fade)
        sink.write(frame)
        if progress and fi - last_rep >= 300:
            last_rep = fi
            el = max(1e-6, time.time() - t_start)
            fps = (fi - f0 + 1) / el
            print("   %d/%d  %.1f fps  eta %.0fs" % (fi + 1, tl["frames"], fps,
                                                     (f1 - fi - 1) / max(fps, 1e-6)), flush=True)
    return sink.close()


def _seg_usable(path, frames):
    """已有分块能不能复用：体积够 + 头部声明的帧数与计划一致。

    为什么不能只看 size>1024：Ctrl-C / 杀进程留下的半截 mp4 也 >1024，被当成
    "已存在"跳过后，拼接只在最后报一句"拼接后时长 0.07s != 预期 29.00s"。
    分块先写 .part 再 os.replace，正常中断不会留下半截正式文件；这里
    再补一道头部 nb_frames 比对，挡住"文件在/大小对但帧数不对"的异常（例如被
    外部工具截断过）。

    注意：**不做** -count_frames 逐帧核对。实测 390 帧的块要 3.5 s（只读头部
    0.2 s），长片几十个块会让每次 --resume 多花几分钟；被截断的 mp4 头部仍写着
    完整 nb_frames，逐帧核对才是唯一能查出来的手段，代价与收益不成比例——真遇到
    这种情况由拼接后的时长校验兜底，并在报错里指明怎么重来。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= 1024:
            return False
        info = K.probe(path) or {}
        for st in info.get("streams") or []:
            if st.get("codec_type") == "video" and st.get("nb_frames"):
                return int(st["nb_frames"]) == int(frames)
        # 读不出 nb_frames（老封装 / ffprobe 版本差异）：退回到"体积够就信"
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _render_chunk_worker(args):
    """分块渲染工作单元（供 multiprocessing 调用；函数必须模块级可 pickle）。"""
    (tl_path, scenes_path, script_path, f0, f1, out_path, crf, preset, part_path) = args[:9]
    work = args[9] if len(args) > 9 else ""
    tl = json.load(open(tl_path, encoding="utf-8"))
    script = json.load(open(script_path, encoding="utf-8"))
    script["_total"] = tl["total"]
    # 不能直接用 JSON 快照里的计划：JSON 会把元组变成 list（主题色是 (r,g,b) 元组），
    # 子进程里 scenes._bg() 拿 th["bg"] 当缓存键 -> TypeError: unhashable type: "list"，
    # 于是 --jobs >1 整条并行路径直接崩。计划是**纯函数**，这里按剧本重新解一次，
    # 既拿回元组，也保证子进程的主题与父进程逐字段一致。
    #
    # 重解时**必须带上 work**。design 路径的身份在 work/design.json 里，
    # 不带 work 就只能走 fail-closed 报错 -> 画出一套完全不同的主题。
    # 实测后果：--jobs 4 的成片与 --jobs 1 / QA 参考帧 MAE 0.059、VMAF 直接掉到 1.49。
    # 这种"并行度不同就出不同片子"的 bug 最隐蔽——单线程跑一遍永远看不见。
    script.pop("_style", None)
    try:
        script["_style"] = resolve_style(script, work or None, verbose=False)
    except Exception as e:                                  # noqa: BLE001
        print("   [WARN] 子进程重解风格失败，回退到快照里的 _style：%s" % e)
        script["_style"] = json.load(open(script_path, encoding="utf-8")).get("_style")
    mod = load_module(scenes_path, "film_scenes_chunk_%d" % f0)
    # 先写 .part 再改名：中途被 Ctrl-C / 被杀时，目标文件名对应的永远是一段**完整**
    # 编码结果。否则半截 mp4 只要 >1024 字节就会被 --resume 当成"已存在"跳过，
    # 拼接后时长对不上才报错（实测：截断到 60 KB 的块 -> 拼接后 0.07s vs 预期 29.00s）。
    _render_range(tl, mod, script, f0, f1, part_path, crf, preset, progress=False)
    os.replace(part_path, out_path)
    return out_path


def st_frames(tl, work, scenes_mod, script, jobs=1, resume=True, engine="pil"):
    print("== Frames（engine=%s）" % engine)
    if engine == "web":
        return _st_frames_web(tl, work, script, jobs=jobs, resume=resume)
    script = dict(script)
    script["_total"] = tl["total"]           # 场景算全局进度条要用
    # 分块 worker 是 spawn 出来的新进程：主题必须随快照走，否则子进程算出的
    # _theme_for 与父进程不一致 -> 拼接处出现"半部片换了配色"。
    script.setdefault("_style", resolve_style(script, work))
    crf = int(script.get("crf", 18))
    preset = script.get("preset", "medium")
    total_frames = tl["frames"]

    if int(jobs) <= 1:
        silent = os.path.join(work, "silent.mp4")
        # 断点续跑：顶层 single 渲染不做帧级续跑（用分块模式才有意义）
        if resume:
            print("   [提示] --jobs 1 是单进程整片渲染，--resume 不生效；要断点续跑请用 --jobs N（N>1）")
        _render_range(tl, scenes_mod, script, 0, total_frames, silent, crf, preset)
        return silent

    # ---- 分块并行：按绝对起始帧命名，便于缺口检测 ----
    seg_dir = os.path.join(work, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    jobs = max(1, int(jobs))
    chunk = int(script.get("chunk_frames") or 0) or \
        int(round(total_frames / float(jobs) / float(tl["fps"]) / 4.0)) * 4 * int(tl["fps"])
    chunk = max(int(tl["fps"]) * 2, int(round(chunk / int(tl["fps"]))) * int(tl["fps"]))
    plan = []
    f = 0
    while f < total_frames:
        f1 = min(total_frames, f + chunk)
        plan.append((f, f1))
        f = f1
    print("   分块: %d 段 x 约 %d 帧，并行 %d" % (len(plan), chunk, jobs))

    script_path = os.path.join(work, "_script_snapshot.json")
    json.dump(script, open(script_path, "w", encoding="utf-8"), ensure_ascii=False)
    tl_path = os.path.join(work, "timeline.json")
    scenes_path = script.get("_scenes") or os.path.join(script["_dir"], "scenes.py")

    # ---- 断点续跑的前置校验：计划变了就不许复用旧分块 ----
    stamp = _frames_plan_stamp(tl, script, scenes_path, chunk)
    stamp_path = os.path.join(seg_dir, "_plan.json")
    old_stamp = None
    if os.path.exists(stamp_path):
        try:
            old_stamp = (json.load(open(stamp_path, encoding="utf-8")) or {}).get("stamp")
        except Exception:                                   # noqa: BLE001
            old_stamp = "unreadable"
    if resume and old_stamp and old_stamp != stamp:
        raise SystemExit(
            "[断点续跑] 分块计划已变化（timeline / scenes.py / 剧本 / 编码参数改过），\n"
            "  旧分块会画出与当前时间轴不符的画面，拒绝拼接。\n"
            "  确认要重渲就删掉分块目录再跑：rm -rf %s" % seg_dir)
    with open(stamp_path, "w", encoding="utf-8") as fh:
        json.dump(dict(stamp=stamp, total=tl["total"], frames=tl["frames"], chunk=chunk),
                  fh, ensure_ascii=False)

    todo = []
    planned = set()
    # 上次被中断留下的半成品（编码中途的残片）：不算分块，直接清掉，别让它占盘。
    # 名字带 _part_ 前缀，既不会被 stale 检测当成"不属于当前计划的旧分块"，
    # 也不会被 _seg_usable 当成"已存在的分块"跳过。
    for n in os.listdir(seg_dir):
        if n.startswith("_part_"):
            try:
                os.remove(os.path.join(seg_dir, n))
            except OSError:
                pass
    for (f0, f1) in plan:
        seg = os.path.join(seg_dir, "seg_%06d_%06d.mp4" % (f0, f1))
        planned.add(os.path.basename(seg))
        if resume and _seg_usable(seg, int(f1) - int(f0)):
            print("   skip 段 %d-%d（已存在）" % (f0, f1))
            continue
        if os.path.exists(seg):
            # 存在但不可用（半截 / 时长不符）：删掉重渲，别让它混进拼接
            print("   [WARN] 段 %d-%d 已存在但帧数/时长对不上，重渲（%d 字节）"
                  % (f0, f1, os.path.getsize(seg)))
            try:
                os.remove(seg)
            except OSError:
                pass
        part = os.path.join(seg_dir, "_part_" + os.path.basename(seg))
        todo.append((tl_path, scenes_path, script_path, f0, f1, seg, crf, preset, part,
                     str(work)))

    # 旧计划留下的分块不会参与拼接，但会一直占盘：说出来，不静默
    stale = [n for n in os.listdir(seg_dir)
             if n.startswith("seg_") and n.endswith(".mp4") and n not in planned]
    if stale:
        print("   [WARN] 分块目录里有 %d 个不属于当前计划的旧文件（不会拼接，可手动删除）" % len(stale))

    if todo:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        ctx = mp.get_context("spawn")
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as ex:
            futs = {ex.submit(_render_chunk_worker, a): a for a in todo}
            done = 0
            for fu in futs:
                fu.result()
                done += 1
                print("   段完成 %d/%d  %.0fs" % (done, len(todo), time.time() - t0), flush=True)

    # ---- 缺口检测 + 拼接 ----
    parts = []
    missing = []
    for (f0, f1) in plan:
        seg = os.path.join(seg_dir, "seg_%06d_%06d.mp4" % (f0, f1))
        if os.path.exists(seg) and os.path.getsize(seg) > 1024:
            parts.append(seg)
        else:
            missing.append((f0, f1))
    if missing:
        raise RuntimeError("分块缺口，不许拼接：%s" % missing[:5])
    silent = os.path.join(work, "silent.mp4")
    K.concat_segments(parts, silent, workdir=work)
    got = K.media_dur(silent)
    want = total_frames / float(tl["fps"])
    if abs(got - want) > 0.5:
        raise RuntimeError("拼接后时长 %.2fs != 预期 %.2fs" % (got, want))
    print("   silent.mp4 %.2fs（%.2f 帧当量）" % (got, got * float(tl["fps"])))
    return silent


def _web_style_payload(we, script, tl=None, work=None):
    """把风格包解析成 webengine 能吃的 __STYLE__ 载荷。

    style=None（legacy 项目 / 解析失败）时返回 None —— webengine 收到 None 连
    add_init_script 都不调，页面行为逐字节不变（这是兼容性硬要求）。
    """
    try:
        style_plan = resolve_style(script, work, verbose=False)
    # 必须连 SystemExit 一起接：resolve_style 在"没有设计/主题不过体检"时用 SystemExit
    # 报错（这是它向 CLI 报用法错误的方式），而 SystemExit 继承自 BaseException，
    # 只 catch Exception 接不住 —— web 页面会拿着半截载荷渲染，或者整个进程直接退出。
    except (Exception, SystemExit) as e:                    # noqa: BLE001
        print("   [WARN] web 风格包解析失败，页面用内置配色：%s" % e)
        return None
    if not style_plan or style_plan.get("legacy"):
        return None
    vp = (tl or {}).get("video") or {}
    try:
        return we.normalize_style(style_plan,
                                  width=int(vp.get("out_w") or K.W),
                                  height=int(vp.get("out_h") or K.H),
                                  fps=int((tl or {}).get("fps") or K.FPS))
    except Exception as e:                                  # noqa: BLE001
        # 载荷不合法要**说出来**：静默回落会让"换了风格但页面没变"变成一个谜。
        print("   [WARN] web 风格载荷不可用（%s），页面用内置配色" % e)
        return None


def _st_frames_web(tl, work, script, jobs=1, resume=True):
    """浏览器逐帧渲染：交给 webengine.render_full（分块并行 + 断点续跑 + 缺口检测）。

    页面的 window.DURATION / window.FPS 是切块与校验的唯一真值来源：
    和 timeline.json 对不上就直接报错（十有八九是改了剧本没重新 layout）。
    把风格包与画幅/帧率一起注入页面（window.__STYLE__），页面颜色不再写死。
    """
    we = _opt_module("webengine",
                     "浏览器渲染需要 webengine.py（本技能 scripts/ 里自带；还需要 pip install playwright）")
    entry = _web_entry(script)
    silent = os.path.join(work, "silent.mp4")
    web = script.get("web") or {}
    style = _web_style_payload(we, script, tl, work)
    vp = tl.get("video") or {}
    canvas_w = int(vp.get("out_w") or K.W)
    canvas_h = int(vp.get("out_h") or K.H)
    if style is not None:
        print("   风格注入: %s（%dx%d @ %s fps）"
              % (style.get("style") or style.get("theme_id") or "custom",
                 canvas_w, canvas_h, tl.get("fps")))

    page_dur, page_fps = we.page_meta(entry, style=style,
                                      width=canvas_w, height=canvas_h,
                                      fps=int(tl["fps"]))
    if abs(float(page_fps) - float(tl["fps"])) > 1e-6:
        raise RuntimeError(
            "页面 FPS=%s 与 timeline fps=%s 不一致。浏览器逐帧渲染的帧号就是 f/FPS，"
            "两边必须同帧率：把页面里的 window.FPS 改成 %s（或重新 --stage layout 生成"
            "与页面一致的 timeline）。\n  %s"
            % (page_fps, tl["fps"], tl["fps"], _web_where(entry)))
    if abs(float(page_dur) - float(tl["total"])) > 0.05:
        raise RuntimeError(
            "页面 DURATION=%.3fs 与 timeline 总时长 %.3fs 不一致（差 %.3fs）。\n"
            "  页面里的时长必须等于 timeline.total；把页面里的 window.DURATION 改成 %.3f"
            "（跑过新的 --stage layout 之后，取 timeline.json 的 total）。\n  %s"
            % (float(page_dur), float(tl["total"]), abs(float(page_dur) - float(tl["total"])),
               float(tl["total"]), _web_where(entry)))

    out = we.render_full(entry, silent, float(tl["total"]),
                         workers=max(1, int(jobs)),
                         chunk_s=float(web.get("chunk_s", 30.0)),
                         resume=bool(resume),
                         crf=int(script.get("crf", 17)),
                         preset=str(script.get("preset", "slow")),
                         style=style, width=canvas_w, height=canvas_h,
                         fps=int(tl["fps"]))
    got = K.media_dur(str(out) if out else silent)
    want = float(tl["frames"]) / float(tl["fps"])
    if abs(got - want) > 0.5:
        raise RuntimeError("浏览器渲染时长 %.2fs != 预期 %.2fs（检查分块缺口/页面 DURATION）"
                           % (got, want))
    print("   silent.mp4 %.2fs（浏览器逐帧，%s→%s 分块）" % (got, page_dur, page_fps))
    return str(out) if out else silent


# ---------------------------------------------------------------- 合成 / 质检
def st_mux(silent, audio, out, script):
    print("== Mux")
    # 真峰目标可覆盖：默认 -1.5 dBTP 是**按横屏实测**定的（AAC 编码会再抬 ~0.9 dB）。
    # 竖屏/换配乐后叠加峰值会变：实测一部竖屏片 audio.wav 峰值 0.872（横屏 0.839），
    # 同样用 -1.5 目标，AAC 之后真峰报 -0.0 dBTP，G5 直接 fail。混音侧的 peak_guard
    # 只保证"不削波"，管不到编码后的真峰——这个参数就是给那条差距留的出口。
    # 只接受负数（>=0 没有物理意义，会把真峰目标推到满刻度）。
    tp = -1.5
    mux_cfg = script.get("mux") if isinstance(script.get("mux"), dict) else {}
    if mux_cfg.get("true_peak_db") is not None:
        try:
            tp = float(mux_cfg["true_peak_db"])
        except (TypeError, ValueError):
            raise SystemExit("[mux] film.json 的 mux.true_peak_db 必须是数字，收到 %r"
                             % (mux_cfg.get("true_peak_db"),)) from None
        if tp >= 0:
            raise SystemExit("[mux] mux.true_peak_db 必须 < 0（收到 %.2f）：真峰目标 >= 0 等于"
                             "放弃余量，AAC 编码后必然超标" % tp)
        print("   真峰目标: %.1f dBTP（film.json mux.true_peak_db）" % tp)
    K.mux(silent, audio, out, crf=int(script.get("crf", 18)),
          preset=script.get("preset", "medium"),
          target_tp=tp,
          reencode_video=bool(script.get("reencode_video", False)))
    print("   ->", out)
    return out


def _qa_gates(lg, rep, deep, tl, work):
    """把 qa_report / 深检的结论拆成逐条门禁记进账本（pass / fail / skipped）。

    为什么拆：只有一个"质检通过/失败"的总结论，读报告的人看不出"哪几项其实没执行"。
    这里每条都带实测值，skipped 必须写明"为什么没执行"。
    """
    pr = rep.get("probe") or {}
    streams = pr.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = pr.get("format") or {}
    # 容器门禁比对的是**交付规格**（timeline 里记的 video 块），不是引擎内部常量。
    # 没有 video 字段的 timeline -> 回落到 1920x1080。
    _vp = tl.get("video") or {}
    W = int(_vp.get("out_w") or K.W)
    H = int(_vp.get("out_h") or K.H)
    FPS = float(_vp.get("fps") or tl.get("fps") or K.FPS)

    # ---- G4 容器：分辨率 / 帧率 / 音轨 / 时长 ----
    try:
        rate = str(v.get("r_frame_rate") or "")
        num, _, den = rate.partition("/")
        got_fps = (float(num) / float(den or 1)) if num and den else 0.0
    except Exception:                                       # noqa: BLE001
        got_fps = 0.0
    dur = float(fmt.get("duration") or 0)
    want_dur = float(tl["frames"]) / float(tl["fps"])
    detail = "%sx%s@%.3g / %s%s / %.2fs" % (
        v.get("width"), v.get("height"), got_fps,
        ("h264" if v.get("codec_name") == "h264" else str(v.get("codec_name") or "?")),
        ("+aac" if a else "（无音轨）"),
        dur)
    ok4 = (v.get("width") == W and v.get("height") == H
           and abs(got_fps - float(FPS)) <= 0.05 and a is not None
           and abs(dur - want_dur) <= 0.5)
    (lg.ok if ok4 else lg.fail)("G4", "容器层（分辨率/帧率/音轨/时长）", detail)

    # ---- G5 响度 / 真峰 ----
    lo = rep.get("loudness") or {}
    if "I" in lo and "TP" in lo:
        i_l, tp = float(lo["I"]), float(lo["TP"])
        ok5 = (-19 <= i_l <= -12) and tp <= -1.0
        (lg.ok if ok5 else lg.fail)("G5", "响度 / 真峰（-16 LUFS / ≤ -1 dBTP）",
                                    "%.1f LUFS · %.1f dBTP" % (i_l, tp))
    else:
        lg.skip("G5", "响度 / 真峰", "ebur128 没测到 %s" % ("I" if "I" not in lo else "TP"))

    # ---- G6 编码锐度比 ----
    q = rep.get("quality")
    if q and "ratio" in q:
        (lg.ok if q["ratio"] >= 0.85 else lg.fail)(
            "G6", "编码锐度比（文字发虚）", "%.3f（阈值 0.85）" % q["ratio"])
    else:
        why = "没装 cv2" if K._cv2() is None else "没有参考帧（scenes.py 缺失 / web 引擎）"
        lg.skip("G6", "编码锐度比（文字发虚）", why + "，本次没查")

    # ---- G7 字幕条数（静默丢字幕的唯一检查点）----
    srt = os.path.join(str(work), "sub.srt")
    got = 0
    if os.path.exists(srt):
        got = len([b for b in open(srt, encoding="utf-8").read().strip().split("\n\n") if b.strip()])
    want = len(tl["scenes"])
    if not os.path.exists(srt):
        # 文件根本没有：sub 阶段没跑过，记 skipped（skipped != 通过）。
        lg.skip("G7", "字幕条数 = 剧本句数", "没找到 sub.srt（sub 阶段没跑）")
    elif got == 0 and want > 0:
        # 文件在但是空的：这是"字幕全丢"，必须 fail。以前落到 else 记成 skipped，
        # 报告里写"没找到 sub.srt"——文件明明在，结论在撒谎。
        lg.fail("G7", "字幕条数 = 剧本句数", "sub.srt 存在但为空：0 / %d 条字幕" % want)
    else:
        (lg.ok if got == want else lg.fail)("G7", "字幕条数 = 剧本句数",
                                            "%d / %d" % (got, want))

    # ---- G8 黑帧 ----
    # G8 读结构化字段 black_segments（None=没执行 / >0=真检测到 / 0=干净）。
    # 以前靠 warnings 里有没有"黑帧"两个字来判断，于是"检测未执行"和"检测到 2 段"
    # 被同一个分支记成 skipped——真发现长黑帧也写不进 fail，门禁等于不存在。
    if "black_segments" not in rep:
        black_n = None
    else:
        black_n = rep.get("black_segments")
    if black_n is None:
        lg.skip("G8", "首尾之外无长黑帧", "检测未执行（blackdetect 失败），本次没查")
    elif int(black_n) > 0:
        lg.fail("G8", "首尾之外无长黑帧", "检测到 %d 段长黑帧" % int(black_n))
    else:
        lg.ok("G8", "首尾之外无长黑帧", "0 段")

    # ---- G10 词级时间戳：覆盖率 / 单调 / 末词偏差 ----
    _word_gate(lg, tl, work)

    # ---- G9 帧级深检（闪烁 / 空窗 / 音画同步）----
    if deep is None:
        lg.skip("G9", "帧级深检（闪烁/空窗/同步）", "没有 qa_frames.py 或深检失败")
    else:
        nf = len(deep.get("flicker") or [])
        nb = len(deep.get("black_windows") or [])
        ns = len(deep.get("sync") or [])
        judged = (deep.get("sync_info") or {}).get("judged")
        sync_note = ("同步查了 %s 条" % judged) if judged is not None else "同步未判（没写 visual_at）"
        # 门禁要按 **deep.errors** 判，而不是按 flicker 条数。
        # qa_frames 会把"块内像素有增有减"（文字入场/母题动画/镜头缓推）的候选从
        # flicker 降级进 warnings（见 FLICKER_AGREE_MIN），此时 flicker 仍非空但已不成问题；
        # 按条数判会让降级形同虚设（零拷贝首片实测：降级后仍判 fail）。
        # errors 里同时也包含空窗与同步超差，所以只看 errors 不会漏判。
        n_err = len([e for e in (deep.get("errors") or []) if e])
        ok9 = n_err == 0
        detail9 = "问题 %d 条（闪烁候选 %d 已按符号一致度分档）· 空窗 %d · 同步超差 %d（%s）" % (
            n_err, nf, nb, ns, sync_note)
        if ok9 and not judged and os.environ.get("QA_REQUIRE_SYNC"):
            # 与 SKILL 的 QA_REQUIRE_SYNC 同义：要求"真的查过"，而不是"没报错"
            lg.fail("G9", "帧级深检（闪烁/空窗/同步超差）",
                    detail9 + "；QA_REQUIRE_SYNC=1 要求同步必须被判过")
        else:
            (lg.ok if ok9 else lg.fail)("G9", "帧级深检（闪烁/空窗/同步超差）", detail9)


WORD_TAIL_MAX = 0.40          # 末词终点到句尾静音的上限（实测模板 0.07–0.12 s）
WORD_START_MAX = 0.20         # 句首到首词的上限（裁剪把开头静音切掉后通常 ≤ 0.05 s）
WORD_COVER_MIN = 0.45         # 词时长之和 / 句实测时长：中文有词间空隙，0.45 是"明显少了"的线


def _word_gate(lg, tl, work):
    """词级时间戳门禁。

    为什么值得单独一道闸：词级数据是"字幕按词折行 / cue 词锚点"的依据，
    但它来自一个**逆向接口**（edge-tts）。服务端一改，最可能的表现是"仍返回音频、
    但词事件为空或整体偏移"——不查就永远发现不了，而画面上只表现为字幕断行变丑。
    三档语义：没有启用 / 没有 words.json -> skipped（写明原因），不许冒充 pass。
    """
    wp = os.path.join(str(work), "words.json")
    if not tl.get("words"):
        lg.skip("G10", "词级时间戳（字幕断行/词锚点依据）",
                "本片未启用（film.json words=false 或 words.json 缺失）")
        return
    if not os.path.exists(wp):
        lg.skip("G10", "词级时间戳（字幕断行/词锚点依据）", "没有 words.json（跑 --stage tts 生成）")
        return
    try:
        wj = json.load(open(wp, encoding="utf-8"))
    except Exception as e:                                  # noqa: BLE001
        lg.fail("G10", "词级时间戳（字幕断行/词锚点依据）", "words.json 读不了：%s" % e)
        return
    lines = wj.get("lines") or {}
    empty = [lid for lid, r in lines.items() if not r.get("n")]
    n_words = sum(int(r.get("n") or 0) for r in lines.values())
    if not lines or n_words == 0:
        lg.skip("G10", "词级时间戳（字幕断行/词锚点依据）",
                "词事件为空（edge-tts 只回了音频；本次降级为等分/整句字幕）")
        return
    bad = []
    if empty:
        bad.append("%d 句没有词事件（%s）" % (len(empty), "、".join(empty[:4])))
    mono_bad = [lid for lid, r in lines.items() if not r.get("mono", True)]
    if mono_bad:
        bad.append("词偏移非单调：%s" % "、".join(mono_bad[:4]))
    tail_bad = [(lid, float(r.get("tail") or 0.0)) for lid, r in lines.items()
                if r.get("n") and float(r.get("tail") or 0.0) > WORD_TAIL_MAX]
    if tail_bad:
        bad.append("末词终点离句尾过远（>%.2fs）：%s"
                   % (WORD_TAIL_MAX, "、".join("%s %.2fs" % x for x in tail_bad[:3])))
    start_bad = [(lid, float(r.get("start") or 0.0)) for lid, r in lines.items()
                 if r.get("n") and float(r.get("start") or 0.0) > WORD_START_MAX]
    if start_bad:
        bad.append("首词起点离句首太远（>%.2fs）：%s"
                   % (WORD_START_MAX, "、".join("%s %.2fs" % x for x in start_bad[:3])))
    cover_bad = []
    for lid, r in lines.items():
        dur = float(r.get("dur") or 0.0)
        if not r.get("n") or dur <= 0:
            continue
        cov = float(r.get("covered") or 0.0) / dur
        if cov < WORD_COVER_MIN:
            cover_bad.append((lid, cov))
    if cover_bad:
        bad.append("词覆盖率过低（<%.0f%%）：%s"
                   % (WORD_COVER_MIN * 100,
                      "、".join("%s %.0f%%" % (l, c * 100) for l, c in cover_bad[:3])))
    tail_max = max([float(r.get("tail") or 0.0) for r in lines.values()] or [0.0])
    detail = "%d 句 / %d 词 · 末词偏差最大 %.2fs" % (len(lines), n_words, tail_max)
    if bad:
        lg.fail("G10", "词级时间戳（字幕断行/词锚点依据）", detail + "；" + "；".join(bad))
    else:
        lg.ok("G10", "词级时间戳（字幕断行/词锚点依据）", detail)


# ⚠ 阈值**只有一套**：权威定义在 qavmaf.py（VMAF_PASS=92 / VMAF_WARN=96，SSIM 仅诊断）。
# 这里刻意不设第二套常量——本仓库的规矩是"同一事实只能有一个权威值"。
# 真实标定（qavmaf 作者实测，参考帧含字幕与片头片尾淡入淡出）：
#   模板片 crf17 VMAF 97.24(pass) / crf30 95.74(pass+warning) / 二次编码坏片 29.45(fail)
# 想按片收紧或放宽就写 film.json 的 qa.vmaf.pass / qa.vmaf.warn，会透传给 qavmaf。
def _qa_vmaf_gate(lg, film, tl, work, scenes_mod, script, mode=""):
    """全片 VMAF/SSIM 门禁：参考帧由场景函数实时渲染，与成片逐帧比较。

    为什么值得全片跑（用户明确要求"全片 + 阻断"）：锐度比只在 4 个时刻采样，
    能发现"整体发虚"，但发现不了"某 30 秒里字幕糊了 / 某段梯度出现色带"。
    VMAF 是唯一能覆盖整条时间轴的客观指标。

    三档语义：
      * 没有 qavmaf.py / 没有 scenes.py（web 引擎）/ 显式 mode=off -> skipped，写明原因；
      * 分数低于 pass 阈值 -> fail（阻断交付）；
      * 介于 pass 与 warn 之间 -> pass + warning（能出片，但建议降 CRF）。
    """
    qv = _try_module("qavmaf")
    if mode == "off":
        lg.skip("G11", "全片 VMAF / SSIM 画质", "本次显式关闭（--vmaf off 或 qa.vmaf.mode=off）")
        return None
    if qv is None:
        lg.skip("G11", "全片 VMAF / SSIM 画质", "没有 qavmaf.py（拷一份进来才能做全片画质门禁）")
        return None
    if scenes_mod is None:
        lg.skip("G11", "全片 VMAF / SSIM 画质",
                "web 引擎/缺 scenes.py：没有可重渲的参考帧（VMAF 参考帧必须来自场景函数）")
        return None
    cfg = ((script or {}).get("qa") or {}).get("vmaf") if isinstance((script or {}).get("qa"), dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    m = str(mode or cfg.get("mode") or "full")
    kw = {}
    for key, ckey in (("vmaf_pass", "pass"), ("vmaf_warn", "warn"),
                      ("ssim_pass", "ssim_pass"), ("ssim_warn", "ssim_warn")):
        if cfg.get(ckey) is not None:
            kw[key] = float(cfg[ckey])
    # 抽样步长：qavmaf.report 的正式参数名是 sample_step（schema 里也是这个名字）。
    # 不传的话 mode=sample 永远按 qavmaf 内部默认步长 5 抽样——用户写了 sample_step
    # 也毫无效果（"配置存在但没人读"正是最容易被忽略的那类静默缺陷）。
    if cfg.get("sample_step") is not None:
        kw["sample_step"] = max(1, int(cfg["sample_step"]))
    if cfg.get("ssim") and isinstance(cfg["ssim"], dict):
        for key, ckey in (("ssim_pass", "pass"), ("ssim_warn", "warn")):
            if cfg["ssim"].get(ckey) is not None:
                kw[key] = float(cfg["ssim"][ckey])
    print("   [vmaf] 模式=%s（参考帧=场景函数实时渲染，不是成片抽帧）" % m)
    # qavmaf 是独立解析（它拿不到本进程的 script["_style"]），所以这里把
    # **冻结设计**一起传下去：参考帧必须与成片同源，否则 VMAF 会掉到个位数。
    # 实测未修前：design 片被解析成 tech-deep，VMAF 1.50、同源抽查 MAE=0.0384。
    script_for_vmaf = dict(script or {})
    dpath = os.path.join(str(work), "design.json")
    if "design" not in script_for_vmaf and os.path.exists(dpath):
        try:
            with open(dpath, encoding="utf-8") as fh:
                script_for_vmaf["design"] = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:        # noqa: BLE001
            print("   [WARN][vmaf] design.json 读不出来，参考帧可能不同源：%s" % e)
    try:
        res = qv.report(film, tl, scenes_mod, script_for_vmaf, mode=m,
                        out_json=os.path.join(str(work), "qa", "vmaf.json"), **kw)
    except Exception as e:                                  # noqa: BLE001
        lg.fail("G11", "全片 VMAF / SSIM 画质", "门禁自身失败：%s" % e)
        print("   [FAIL][vmaf]", e)
        return None
    gate = (res or {}).get("gate") or {}
    v = (res or {}).get("vmaf") or {}
    s = (res or {}).get("ssim") or {}
    bits = []
    if v.get("score") is not None:
        bits.append("VMAF %.2f" % float(v["score"]))
    if s.get("score") is not None:
        bits.append("SSIM %.4f" % float(s["score"]))
    bits.append("%s 帧" % ((res or {}).get("frames_compared") or 0))
    detail = " · ".join(bits)
    if (res or {}).get("elapsed_s") is not None:
        detail += " · 用时 %.1fs" % float(res["elapsed_s"])
    st = str(gate.get("status") or "skipped")
    for w in (res or {}).get("warnings") or []:
        print("   [WARN][vmaf] %s" % w)
    for e in (res or {}).get("errors") or []:
        print("   [FAIL][vmaf] %s" % e)
    if st == "fail":
        lg.fail("G11", "全片 VMAF / SSIM 画质", detail + "；" + str(gate.get("detail") or ""))
    elif st == "skipped":
        lg.skip("G11", "全片 VMAF / SSIM 画质", str(gate.get("detail") or "本次没跑"))
    else:
        lg.ok("G11", "全片 VMAF / SSIM 画质", detail)
    print("   [vmaf] %s %s" % (st, detail))
    return res


# 实测标定（同一部模板片，29 s，faster-whisper small / CPU int8）：
#   干净成片：字符召回 0.828（专有名词 "DeepSeek 4.1 Flash" 被听成 DIXX/DeepSeek 变体，
#            4 段全对上、缺句 0）—— 这不是流水线坏了，是 ASR 对中英混排专名的正常误差。
#   所以默认线取 0.85 / 0.72：**低于 0.72 才说明"整句丢了/音轨跟剧本不是一套"**，
#   0.72–0.85 之间给 warning（仍然出片，但提示可能串了缓存）。
# 想更严就按片覆盖 film.json 的 qa.asr.recall_pass / recall_warn。
QA_ASR_RECALL_PASS = 0.85
QA_ASR_RECALL_WARN = 0.72


def _qa_asr_gate(lg, film, work, script, tl=None, cfg=None):
    """ASR 回读门禁：把成片音轨转回文字，和剧本逐句对账。

    为什么值得：容器/响度/帧级质检都默认"音轨里念的就是剧本那几句"。TTS 缓存串了、
    换声线漏句、混音压没了、音轨是旧的——这些从波形和容器上都看不出来，只有转回
    文字对账才能发现。代价是每片多 4–6 s（small 模型，CPU），可关。
    """
    qa = cfg if isinstance(cfg, dict) else {}
    a = qa.get("asr") if isinstance(qa.get("asr"), dict) else {}
    if a.get("enabled") is False:
        lg.skip("G12", "ASR 回读（原稿 vs 成片旁白）",
                "本次显式关闭（qa.asr.enabled=false）")
        return None
    qa_mod = _try_module("qaasr")
    if qa_mod is None:
        lg.skip("G12", "ASR 回读（原稿 vs 成片旁白）", "没有 qaasr.py（拷一份进来才能做回读）")
        return None
    kw = dict(recall_pass=QA_ASR_RECALL_PASS, recall_warn=QA_ASR_RECALL_WARN)
    if a.get("recall_pass") is not None:
        kw["recall_pass"] = float(a["recall_pass"])
    if a.get("recall_warn") is not None:
        kw["recall_warn"] = float(a["recall_warn"])
    if a.get("model"):
        kw["model_size"] = str(a["model"])
    print("   [asr] 回读成片音轨（模型 %s，CPU）" % (kw.get("model_size") or "small"))
    try:
        # 偏移判据的期望开口时刻由 qaasr 从 timeline.scenes[].words_abs 直接读（那是
        # TTS 自己报的首词绝对时刻）。这里**不**再传 work/words.json：它的 words[] 是
        # 段内相对时间，当绝对秒用会得到荒谬结论（实测过 -202s）。
        res = qa_mod.report(film, tl, script,
                            out_json=os.path.join(str(work), "qa", "asr.json"), **kw)
    except Exception as e:                                  # noqa: BLE001
        lg.fail("G12", "ASR 回读（原稿 vs 成片旁白）", "门禁自身失败：%s" % e)
        print("   [FAIL][asr]", e)
        return None
    gate = (res or {}).get("gate") or {}
    detail = "字符召回 %.3f（%s）" % (float((res or {}).get("recall") or 0.0),
                                  (res or {}).get("engine") or "-")
    if (res or {}).get("elapsed_s") is not None:
        detail += " · %.1fs" % float(res["elapsed_s"])
    for w in (res or {}).get("warnings") or []:
        print("   [WARN][asr] %s" % w)
    for e in (res or {}).get("errors") or []:
        print("   [FAIL][asr] %s" % e)
    st = str(gate.get("status") or "skipped")
    if st == "fail":
        lg.fail("G12", "ASR 回读（原稿 vs 成片旁白）", detail + "；" + str(gate.get("detail") or ""))
    elif st == "skipped":
        lg.skip("G12", "ASR 回读（原稿 vs 成片旁白）", str(gate.get("detail") or "本次没跑"))
    else:
        lg.ok("G12", "ASR 回读（原稿 vs 成片旁白）", detail)
    print("   [asr] %s %s" % (st, detail))
    return res


def st_qa(film, tl, work, scenes_mod=None, script=None, vmaf_mode=""):
    print("== QA")
    th = _theme_for(script, work) if script is not None else None
    shots, refs = [], []             # refs: [(frac 下标, PIL 原图)]
    fracs = (0.08, 0.3, 0.55, 0.78)
    ref_script = dict(script) if script is not None else None
    if ref_script is not None:
        # 参考帧必须和 frames 阶段用同一份 script（含 _total），否则 HUD 进度条等
        # 依赖 _total 的元素画得不一样，锐度比会把"渲染差异"当成"编码损失"。
        ref_script["_total"] = float(tl["total"])
    for k, frac in enumerate(fracs):
        sec = tl["total"] * frac
        p = os.path.join(work, "qa", "snap_%02d.png" % int(frac * 100))
        K.snap(film, sec, p)
        shots.append(p)
        # 参考帧：用场景函数重新渲染同一时刻，而不是拿成片自己抽的帧
        if scenes_mod is not None and ref_script is not None:
            idx = 0
            for i, s in enumerate(tl["scenes"]):
                if sec >= s["start"]:
                    idx = i
            s = tl["scenes"][idx]
            try:
                fr = scenes_mod.draw_frame(s, sec - s["start"], s["dur"], sec, ref_script,
                                           int(round(sec * tl["fps"])))
                _hold = ref_script.get("sub_max_hold")
                fr = K.draw_subtitle(fr, K.sub_at(tl["scenes"], sec, _hold), theme=th,
                                     words=K.sub_words_at(tl["scenes"], sec, _hold),
                                     **_sub_kw(ref_script))
                refs.append((k, fr))
            except Exception as e:      # noqa: BLE001
                print("   [WARN] 参考帧渲染失败:", e)

    # 锐度比只跟"同一时刻"的参考帧比：某几张参考帧渲染失败时，不能拿别的时刻顶替
    ref_img, ref_sec = None, None
    if refs:
        k_ref = min(refs, key=lambda kn: abs(kn[0] - 2))[0]
        ref_img = dict(refs)[k_ref]
        ref_sec = tl["total"] * fracs[k_ref]
        print("   锐度参考帧: 场景函数重渲染 t=%.2fs（第 %d 张）" % (ref_sec, k_ref + 1))
    elif scenes_mod is None:
        print("   [WARN] 没有场景函数参考帧（web 引擎或 scenes.py 缺失），锐度比门禁本次跳过")
    _vq = tl.get("video") or {}
    rep = K.qa_report(film, expect_frames=tl["frames"], ref_img=ref_img,
                      ref_sec=ref_sec, expected_subtitles=len(tl["scenes"]),
                      notes=os.path.join(work, "sub.srt"), theme=th,
                      expect_w=int(_vq.get("out_w") or K.W),
                      expect_h=int(_vq.get("out_h") or K.H),
                      expect_fps=float(_vq.get("fps") or tl.get("fps") or K.FPS))
    pr = rep["probe"]
    f = pr.get("format", {})
    print("   时长 %.2fs  大小 %.1f MB  码率 %.2f Mbps" % (
        float(f.get("duration", 0)), int(f.get("size", 0)) / 1048576,
        int(f.get("bit_rate", 0)) / 1e6))
    print("   响度", rep["loudness"])
    q = rep.get("quality")
    if q:
        print("   锐度比 %.2f（参考帧=场景函数重渲染，不是成片抽帧）" % q["ratio"])
    elif ref_img is None and scenes_mod is None:
        print("   [WARN] 锐度比门禁未执行（没有参考帧）")
    safe = K.qa_text_safe(tl["scenes"])
    print("   字幕安全区:", "OK" if not safe else safe[:2])
    # 字幕安全区 / 缺字形若只 print、不进 qa_report 的 warnings，
    # CI、前端、任何只读 JSON 的人都**看不到**这两条（"门禁可以保守，但不能静默"）。
    # 所以它们进 warnings（仍是 warning 级，不阻断，但能被程序读到）。
    for _m in (safe or [])[:4]:
        rep["warnings"].append("字幕安全区: %s" % _m)
    _glyph = K.check_glyphs((script or {}).get("lines") or [])
    for _lid, _fld, _chars in (_glyph or [])[:4]:
        rep["warnings"].append("缺字形 %s.%s: %r（会显示为方块，改用矢量线条画）"
                               % (_lid, _fld, _chars))
    sheet = K.contact_sheet(shots, os.path.join(work, "qa", "sheet.png"))
    if sheet:
        print("   联系表:", sheet, "  <- 必须亲眼看")
    # ---- 帧级深检：单帧闪烁 / 画面空窗 / 音画同步 / 响度曲线 ----
    deep = _qa_deep(film, work, tl, script)
    if deep:
        for w in deep.get("warnings") or []:
            print("   [WARN][deep]", w)
        for e in deep.get("errors") or []:
            print("   [FAIL][deep]", e)

    for w in rep["warnings"]:
        print("   [WARN]", w)
    errors = list(rep["errors"])
    if deep:
        errors += list(deep.get("errors") or [])
    if errors:
        print("   [FAIL]")
        for e in errors:
            print("     -", e)
    else:
        print("   [PASS] 质检通过")
    lg = _gate()
    if lg is not None:
        _qa_gates(lg, rep, deep, tl, work)
        # 全片 VMAF/SSIM 放在最后跑：它最贵（约 1.2× 实时），前面有 fail 时可省下这十分钟。
        _qa_vmaf_gate(lg, film, tl, work, scenes_mod, script, mode=vmaf_mode)
        # ASR 回读最便宜（模板片 6 s），放最后跑：前面已经 fail 的片没必要再花这 6 秒。
        _qa_asr_gate(lg, film, work, script, tl, cfg=((script or {}).get("qa") or {}))
    return errors


def _qa_deep(film, work, tl, script):
    """调用 qa_frames.py 做帧级/时间轴级深检；没有这个模块就跳过（不阻断）。"""
    qf = _try_module("qa_frames")
    if qf is None:
        print("   [deep] 没有 qa_frames.py，跳过帧级深检")
        return None
    qa_dir = os.path.join(work, "qa")
    os.makedirs(qa_dir, exist_ok=True)
    cues_path = os.path.join(work, "cues.json")
    try:
        deep = qf.report(film, cues_json=cues_path if os.path.exists(cues_path) else None,
                         expect_duration=float(tl["total"]),
                         out_json=os.path.join(qa_dir, "deep.json"),
                         out_png=os.path.join(qa_dir, "deep.png"))
    except Exception as e:                                  # noqa: BLE001
        print("   [WARN][deep] 帧级深检失败（不影响成片）:", e)
        return None
    print("   [deep] 闪烁 %d / 空窗 %d / 同步超差 %d  -> %s"
          % (len(deep.get("flicker") or []), len(deep.get("black_windows") or []),
             len(deep.get("sync") or []), os.path.join(qa_dir, "deep.json")))
    return deep


# ---------------------------------------------------------------- 母版预览
def st_stills(tl, work, scenes_mod, script, specs=None):
    """只渲染若干关键帧 -> out/stills/*.png + 联系表。母版阶段必跑。

    specs: ["l01:3.0", ...] 或 [[id, t_local], ...]；缺省为每段中点各一张。
    """
    print("== Stills")
    script = dict(script)
    script["_total"] = tl["total"]
    script.setdefault("_style", resolve_style(script, work))
    outdir = os.path.join(work, "stills")
    os.makedirs(outdir, exist_ok=True)
    by_id = {s["id"]: s for s in tl["scenes"]}
    if not specs:
        specs = [[s["id"], s["dur"] * 0.55] for s in tl["scenes"]]
    made = []
    for spec in specs:
        if isinstance(spec, str):
            sid, _, lt = spec.partition(":")
            lt = float(lt or 0.0)
        else:
            sid, lt = spec[0], float(spec[1])
        s = by_id.get(sid)
        if not s:
            print("   [skip] 没有这个场景:", sid)
            continue
        tg = s["start"] + float(lt)
        fr = scenes_mod.draw_frame(s, float(lt), s["dur"], tg, script,
                                   int(round(tg * tl["fps"])))
        if fr is None:
            raise RuntimeError("场景 %s 在 t=%.2fs 返回 None（draw_frame 必须返回 PIL.Image）"
                               % (sid, tg))
        if tuple(fr.size) != (K.W, K.H):
            raise RuntimeError("场景 %s 在 t=%.2fs 返回 %dx%d，期望 %dx%d（stills 与 FrameSink 一样严格）"
                               % (sid, tg, fr.size[0], fr.size[1], K.W, K.H))
        _hold = script.get("sub_max_hold")
        fr = K.draw_subtitle(fr, K.sub_at(tl["scenes"], tg, _hold),
                             theme=_theme_for(script),
                             words=K.sub_words_at(tl["scenes"], tg, _hold),
                             **_sub_kw(script))
        p = os.path.join(outdir, "%s_%05.1f.png" % (sid, float(lt)))
        fr.save(p)
        made.append(p)
    if made:
        cols = 6 if len(made) >= 6 else len(made)
        sheet = K.contact_sheet(made, os.path.join(outdir, "_sheet.png"), cols=cols, thumb_w=420)
        print("   %d 张静帧 -> %s" % (len(made), outdir))
        print("   联系表:", sheet, "  <- 必须亲眼看")
    return made


def _reload_ledger_from_disk(work, ledger):
    """把 work/gates.json 里已有的门禁条目搬回内存账本（accept 这类独立进程用）。

    只搬**内容指纹一致**的条目：指纹不同说明剧本/场景/引擎已经变过，那些结论属于
    上一版内容，搬进来等于拿旧结论冒充本版（比丢条目更糟）。搬不回来时明确说一句，
    让用户知道报告里的门禁矩阵为什么是空的（该重跑 --stage all）。
    """
    p = os.path.join(str(work), "gates.json")
    if not os.path.exists(p):
        return 0
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print("   [WARN] 旧账本读不了（%s）：报告里只会有本次的条目" % e)
        return 0
    if not isinstance(d, dict):
        return 0
    if str(d.get("contentHash") or "") != str(getattr(ledger, "content_hash", "") or ""):
        print("   [WARN] 旧账本的内容指纹与当前不一致（剧本/场景/引擎改过），"
              "不搬旧门禁结论：交付前请重跑 --stage qa")
        return 0
    n = 0
    for it in d.get("items") or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        try:
            ledger.add(str(it["id"]), str(it.get("title") or ""),
                       str(it.get("status") or ""), str(it.get("detail") or ""),
                       at=it.get("at"))
            n += 1
        except (ValueError, TypeError) as e:
            print("   [WARN] 旧账本条目 %r 搬不回来：%s" % (it.get("id"), e))
    for b in d.get("bypassed") or []:
        if isinstance(b, dict) and b.get("what"):
            ledger.bypassed.append(dict(what=str(b["what"]), why=str(b.get("why") or ""),
                                        at=float(b.get("at") or time.time())))
    return n


def _report_path(a):
    """QC 报告的唯一落点：**站在成片旁边**（--out 的目录），不猜 work 的兄弟目录。

    accept 与 all/mux/qa 必须用同一个函数算路径：若前者用 dirname(abspath(work))/out/、
    后者用 dirname(abspath(out))，--out 指到任何非 out/ 的目录（deliver/、中文工作区/成品/…）
    时，签核更新的是另一份没人交付的文件，交付出去的那份仍是"未签核"——正好打穿
    "报告是交付凭据"这条承诺。
    """
    return os.path.join(os.path.dirname(os.path.abspath(str(a.out))), "QC-REPORT.md")


# 会写交付账本（work/gates.json + QC-REPORT.md）的阶段。中间产物阶段
# （tts/layout/cues/score/sfx/audio）只做自己的事，不去覆盖上一轮完整跑出来的
# 交付报告——否则 `--stage layout` 一次就会把「全片通过」的报告改写成「未判定」。
_PERSIST_STAGES = ("style", "frames", "mux", "qa", "stills", "all", "accept")


def _finish_gates(a, persist=True):
    """一次运行收尾：落 gates.json + 写 QC-REPORT.md（在成片旁边）+ 统一退出码。

    persist=False：只打印本次门禁汇总与退出码，不动磁盘上的交付账本（中间阶段用）。
    """
    lg = _LEDGER
    if lg is None:
        return 0
    errs = list(lg.exit_errors(strict=bool(getattr(a, "strict_gates", False))))
    gj = rep = ""
    if persist:
        # 交付账本只在交付阶段写；中间阶段（layout 等）写一次就会把上一轮
        # 「pass 8 / skipped 1」的完整报告换成「只有本阶段一条」的半成品。
        # 交付凭据写不出去 = 没有交付凭据。以前只打一行 WARN 就继续，退出码仍是 0，
        # 于是"QC-REPORT.md 根本没生成"这件事在机器眼里等于通过。这里把它记成 fail：
        # 账本里有一条明确失败，退出码随之非零（--strict-gates 之外也一样）。
        try:
            gj = lg.write_json()
        except Exception as e:                              # noqa: BLE001
            print("[ERROR] gates.json 写入失败:", e)
            lg.fail("deliver", "交付凭据落盘（gates.json）", "写入失败：%s" % e)
            errs.append("gates.json 写入失败：%s" % e)
        try:
            rep = GT.write_report(a.work, _report_path(a))
        except Exception as e:                              # noqa: BLE001
            print("[ERROR] QC 报告写入失败:", e)
            lg.fail("deliver", "交付凭据落盘（QC-REPORT.md）", "写入失败：%s" % e)
            errs.append("QC-REPORT.md 写入失败：%s" % e)
    c = lg.counts()
    print("")
    print("== 门禁汇总：pass %d · fail %d · skipped %d%s"
          % (c[GT.OK], c[GT.FAIL], c[GT.SKIP],
             "（--strict-gates：skipped 也算失败）" if getattr(a, "strict_gates", False) else ""))
    if gj:
        print("   账本:", gj)
    if rep:
        print("   报告:", rep, " <- 交付时把这个文件一起给出")
        try:
            _run_dir, _err = GT.archive_run(a.work, lg, report_path=rep,
                                            artifacts=[str(a.out) if getattr(a, "out", "") else ""])
            if _run_dir:
                print("   归档:", _run_dir, "(work/runs/%s/)" % lg.run_id)
            elif _err:
                print("   [WARN] 归档失败:", _err)
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] 归档失败:", e)
    if c[GT.SKIP]:
        print("   注意：有 %d 项本次**没有执行**（skipped != 通过）" % c[GT.SKIP])
    if errs:
        print("   [FAIL]")
        for e in errs:
            print("     -", e)
        return 1
    return 0


# ---------------------------------------------------------------- 批量
BATCH_MAX_FILMS = 100
BATCH_MAX_BYTES = 1 << 20            # 1 MiB：清单文件本身不该是个大 JSON


def _load_batch(path):
    """读批量清单 -> [job]。支持两种写法（都接受）：

        ["a/film.json", "b/film.json"]                       # 简写：只给剧本
        {"films": [{"script": "a/film.json", "work": "a/work",
                    "scenes": "a/scenes.py", "out": "out/a.mp4"}]}

    批量最容易死在"第 7 条跑一半才发现清单写错"，所以数量/体积先卡死，
    并且在**开跑之前**全部预检一遍。
    """
    p = str(path)
    if not os.path.exists(p):
        raise SystemExit("[batch] 清单不存在：%s" % p)
    if os.path.getsize(p) > BATCH_MAX_BYTES:
        raise SystemExit("[batch] 清单 %.1f KB 超过 %d KB 上限：拆成几份跑"
                         % (os.path.getsize(p) / 1024.0, BATCH_MAX_BYTES // 1024))
    try:
        data = json.load(open(p, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit("[batch] 清单不是合法 JSON：%s" % e) from e
    rows = data.get("films") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise SystemExit("[batch] 清单必须是数组，或 {\"films\": [...]} 且非空")
    if len(rows) > BATCH_MAX_FILMS:
        raise SystemExit("[batch] %d 条超过 %d 条上限：拆成几份跑"
                         % (len(rows), BATCH_MAX_FILMS))
    base = os.path.dirname(os.path.abspath(p))
    jobs = []
    for i, row in enumerate(rows):
        if isinstance(row, str):
            row = {"script": row}
        if not isinstance(row, dict) or not row.get("script"):
            raise SystemExit("[batch] 第 %d 条缺少 script 字段：%r" % (i + 1, row))

        def _abs(v, default=""):
            if not v:
                return default
            v = str(v)
            return v if os.path.isabs(v) else os.path.normpath(os.path.join(base, v))

        script = _abs(row["script"])
        stem = os.path.splitext(os.path.basename(script))[0]
        jobs.append(dict(
            script=script,
            scenes=_abs(row.get("scenes"), os.path.join(os.path.dirname(script), "scenes.py")),
            work=_abs(row.get("work"), os.path.join(base, "work_" + stem)),
            out=_abs(row.get("out"), os.path.join(base, "out", stem + ".mp4")),
            label=str(row.get("label") or stem),
        ))
    return jobs


def run_batch(a):
    """批量渲染：先全量预检 -> 逐条隔离执行 -> JSON 汇总 + 退出码。

    失败隔离是这套流程的全部价值：一条剧本写错，不该让另外 9 条都不出片。
    但也**不静默**：预检阶段就失败的条目会在开跑前一次性报全，跑的过程中失败的
    条目会带着退出码与日志尾进汇总。
    """
    jobs = _load_batch(a.batch)
    print("== Batch（%d 条）" % len(jobs))
    here = os.path.dirname(os.path.abspath(__file__))
    fc = os.path.join(here, "filmcheck.py")
    checks = []
    bad = 0
    for j in jobs:
        row = dict(label=j["label"], script=j["script"], ok=False, errors=[], warnings=[])
        if not os.path.exists(j["script"]):
            row["errors"].append("剧本不存在：%s" % j["script"])
        elif os.path.exists(fc):
            p = subprocess.run([sys.executable, fc, j["script"], "--json"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            try:
                d = json.loads((p.stdout or "").strip() or "{}")
            except json.JSONDecodeError:
                d = {}
            row["ok"] = bool(d.get("ok", p.returncode == 0))
            row["errors"] = [e.get("msg") if isinstance(e, dict) else str(e)
                             for e in (d.get("errors") or [])][:8]
            row["warnings"] = len(d.get("warnings") or [])
        else:
            row["ok"] = True                     # 没装校验器：不阻断，但汇总里会说
            row["warnings"] = -1
        if not row["ok"]:
            bad += 1
        checks.append(row)
    for r in checks:
        print("   %s %-12s %s" % ("OK  " if r["ok"] else "FAIL", r["label"],
                                  "；".join(r["errors"][:3]) if r["errors"] else ""))
    if bad:
        rep_p = os.path.join(os.path.dirname(os.path.abspath(jobs[0]["work"])), "batch-report.json")
        # 预检阶段 work/ 目录通常还不存在（子进程要跑起来才会建），这里必须先建父目录：
        # 否则"清单里有一条写错"这个**本来只想报错退出**的路径，会先抛 FileNotFoundError，
        # 用户看到的是"[ERROR] 文件/目录操作失败"，既丢了"一条都没跑"的说明，也没有台账。
        os.makedirs(os.path.dirname(rep_p), exist_ok=True)
        json.dump(dict(phase="preflight", films=checks), open(rep_p, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        raise SystemExit("[batch] 预检有 %d 条不通过，**一条都没跑**（清单/剧本先修好）：%s\n"
                         "  明细：%s" % (bad, "、".join(r["label"] for r in checks if not r["ok"]),
                                        rep_p))
    print("   预检全部通过，开始逐条渲染（单条失败不阻断其余）")

    results = []
    for j in jobs:
        print("\n---- [batch] %s ----" % j["label"])
        cmd = [sys.executable, os.path.join(here, "render.py"),
               "--script", j["script"], "--scenes", j["scenes"],
               "--work", j["work"], "--out", j["out"], "--stage", "all"]
        cmd += ["--engine", a.engine] if a.engine else []
        cmd += ["--cost"] if a.cost else []
        cmd += ["--strict-gates"] if a.strict_gates else []
        # --layout 必须透传：父进程的 _early_layout 只作用于父进程，子进程是独立进程，
        # argv 里没有 --layout 就按横屏渲——批量出竖屏片会静默全变横屏。
        cmd += ["--layout", a.layout] if a.layout else []
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        wall = time.time() - t0
        tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-6:]
        results.append(dict(label=j["label"], script=j["script"], out=j["out"],
                            work=j["work"], exit=p.returncode, ok=(p.returncode == 0),
                            wall_s=round(wall, 1), tail=tail))
        print("   %s 用时 %.1fs  -> %s" % ("OK" if p.returncode == 0 else "FAIL", wall, j["out"]))
        if p.returncode != 0:
            for ln in tail:
                print("     | " + ln)

    okn = sum(1 for r in results if r["ok"])
    rep_p = os.path.join(os.path.dirname(os.path.abspath(jobs[0]["work"])), "batch-report.json")
    os.makedirs(os.path.dirname(rep_p), exist_ok=True)
    json.dump(dict(phase="render", films=checks, results=results,
                   summary=dict(total=len(results), ok=okn, failed=len(results) - okn)),
              open(rep_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n== Batch 汇总：%d/%d 成功，%d 失败" % (okn, len(results), len(results) - okn))
    print("   台账：%s" % rep_p)
    for r in results:
        if not r["ok"]:
            print("   FAIL %-12s exit=%s" % (r["label"], r["exit"]))
    return 0 if okn == len(results) else 1


# ---------------------------------------------------------------- 提案闸
def _proposal_gate(a):
    """提案闸：frames 之前必须先有"已确认的设计"。

    本项目的主路径是"先提案、再出片"，所以这条闸是语义要求，不是可选装饰。
    但它默认只警告（0 退出码），加 --require-proposal 才失败——原因只有一条：
    --stage frames 是开发期高频命令（改一帧看一帧），硬拦会让调试变得没法用。

    判定（propose.proposal_state 的几档）：
      absent    -> 没有任何提案
      draft     -> 有提案但没确认
      confirmed -> 已确认且 design.json 指纹与确认时一致（唯一放行态）
      stale     -> 确认过但 design.json 被改过（等于没确认）
    """
    try:
        import propose as PP
    except ImportError:
        print("[proposal] 没有 propose.py，跳过提案检查（只警告）")
        return True
    try:
        status, detail = PP.proposal_state(a.work)
    except SystemExit as e:
        status, detail = "broken", str(e)
    except Exception as e:                                  # noqa: BLE001
        status, detail = "broken", "%s: %s" % (type(e).__name__, e)

    if status == "confirmed":
        print("[proposal] OK  %s" % detail)
        _LEDGER.ok("proposal", "提案已确认", detail)
        return True

    msg = "提案闸未通过（%s）：%s" % (status, detail)
    print("[proposal] WARN  %s" % msg)
    if status in ("absent", "draft"):
        print("           先跑：python propose.py --brief \"题材/要求\" --script %s --n 3" % a.script)
        print("           确认：python propose.py --confirm --candidate A")
    if bool(getattr(a, "require_proposal", False)):
        _LEDGER.fail("proposal", "提案未确认", msg)
        print("\n[提案闸] --require-proposal 下这是失败项：先提案并确认，或去掉该参数")
        return False
    _LEDGER.skip("proposal", "提案闸", msg + "（默认只警告；--require-proposal 才失败）")
    return True


# ---------------------------------------------------------------- 场景模块定位
def _builtin_scenes() -> str:
    """本技能内置的场景绘制模块路径（装成插件后在包内 skills/video-studio/assets/scenes.py）。"""
    cand = (Path(__file__).resolve().parent.parent / "assets" / "scenes.py")
    return str(cand) if cand.exists() else ""


def _resolve_scenes(scenes_arg, script_path) -> str:
    """定位场景模块，按优先级：显式 --scenes > 剧本同目录 scenes.py > 技能内置 scenes.py。

    为什么要这条回退链：要求用户把 assets/scenes.py 拷进工作目录，
    即「提供一份模板代码」——与「不提供任何模板」直接冲突，而且用户每部片都得
    重拷一份 98KB 的绘制代码。所以把内置渲染器当**默认能力**：工作目录里没有 scenes.py
    时直接用它出片；需要改画面再写自己的 scenes.py（显式指定或用同目录约定）。
    """
    if scenes_arg:
        p = Path(scenes_arg).resolve()
        if p.exists():
            return str(p)
        raise SystemExit(
            "[缺少场景模块] --scenes 指向的 %s 不存在。\n"
            "  不写 --scenes 时会自动用剧本同目录的 scenes.py 或技能内置渲染器；"

            "只想用内置的就去掉 --scenes。" % p)
    cand = Path(script_path).resolve().parent / "scenes.py"
    if cand.exists():
        return str(cand)
    builtin = _builtin_scenes()
    if builtin:
        return builtin
    raise SystemExit(
        "[缺少场景模块] 找不到 %s，也找不到技能内置渲染器（%s）。\n"
        "  装成插件时内置渲染器应在 <包>/skills/video-studio/assets/scenes.py；"

        "请检查插件包是否完整，或显式传 --scenes 指向你自己的 scenes.py。" % (cand, _builtin_scenes() or "missing"))


# ---------------------------------------------------------------- main
def main():
    _setup_stdio()
    ap = argparse.ArgumentParser(description="video-studio 编排器")
    ap.add_argument("--script", default="film.json")
    ap.add_argument("--scenes", default="",
                    help="场景绘制模块（默认依次找：<剧本目录>/scenes.py、本技能内置的 scenes.py）")
    ap.add_argument("--out", default="out/film.mp4")
    ap.add_argument("--work", default="work")
    ap.add_argument("--stage", default="all", choices=STAGES)
    ap.add_argument("--engine", default="", choices=("", "pil", "web"),
                    help="渲染引擎：pil=逐帧 Pillow（默认）；web=无头浏览器（需 webengine.py + 页面）")
    ap.add_argument("--jobs", type=int, default=1, help="分块并行渲染进程数（frames 阶段）")
    ap.add_argument("--layout", default="", choices=("", "landscape", "portrait"),
                    help="画布布局：landscape=1920x1080（默认）；portrait=1080x1920（**实验性**，"
                         "版式骨架与 compare/process/chart 的构图内部都已竖屏原生，"
                         "但排版节奏（停留窗口、estimate 耗时基准）未按竖屏复核）")
    ap.add_argument("--resume", action="store_true", help="复用已存在的分块")
    ap.add_argument("--force-tts", action="store_true", help="丢弃 TTS 缓存重新合成")
    ap.add_argument("--force-music", action="store_true", help="丢弃配乐重出（score 阶段）")
    ap.add_argument("--stills", default="", help="stills 阶段用：逗号分隔的 id:秒")
    ap.add_argument("--gate", default="", choices=("",) + GT.GATE_ORDER,
                    help="accept 阶段用：要签核哪一道闸（%s）" % " / ".join(GT.GATE_ORDER))
    ap.add_argument("--verdict", default="approved", choices=("approved", "rejected"),
                    help="accept 阶段用：签核结论（rejected 视为没签）")
    ap.add_argument("--note", default="", help="accept 阶段用：签核备注（写进报告）")
    ap.add_argument("--by", default="", help="accept 阶段用：签核人（缺省 user）")
    ap.add_argument("--strict-gates", action="store_true",
                    help="把 skipped 门禁也算失败（对齐 QA_REQUIRE_SYNC 语义）")
    ap.add_argument("--strict-stale", action="store_true",
                    help="上游产物过时算失败（默认只告警）")
    ap.add_argument("--allow-stale", action="store_true",
                    help="过时也照跑（会在 QC 报告里留痕）")
    ap.add_argument("--no-gate", action="store_true",
                    help="跳过人工签核检查（会在 QC 报告里留痕，不算通过）")
    ap.add_argument("--require-proposal", action="store_true",
                    help="把'提案未确认'当成失败（默认只警告；先跑 propose.py 再出片）")
    ap.add_argument("--cost", action="store_true",
                    help="每个 stage 往 work/cost.jsonl 记一行实测耗时")
    # ---- 批量 / 画质门禁 ----
    ap.add_argument("--batch", default="",
                    help="批量清单（JSON 数组或 {\"films\":[...]}，≤100 条）："
                         "先全量预检，再逐条渲染，单条失败不阻断其余")
    ap.add_argument("--vmaf", default="", choices=("", "full", "sample", "off"),
                    help="qa 阶段的全片 VMAF/SSIM 模式（缺省读 film.json 的 qa.vmaf.mode）")
    ap.add_argument("--no-preflight", action="store_true",
                    help="跳过渲染前资源预检（会在 QC 报告里留痕，不算通过）")
    ap.add_argument("--preview", default="",
                    help="只渲前 N 秒做预览片（出 out/preview/*.mp4 + .gif），不写交付报告")
    ap.add_argument("--run-id", default="",
                    help="本次运行的标签（写进 QC 报告，便于对照多版本）")
    a = ap.parse_args()
    if a.layout:
        _early_layout(["--layout", a.layout])    # 与顶部扫描同一逻辑（--batch 也走这里）
    if (os.environ.get("VIDEO_STUDIO_LAYOUT") or "").strip().lower() in (
            "portrait", "9x16", "vertical", "portrait-9x16"):
        sys.stderr.write(
            "[warn] 竖屏是实验性布局：安全区/字幕带/版式骨架/compare/process 都已按 1080x1920 "
            "原生实现，但**排版节奏**（停留窗口、estimate 耗时基准）没按竖屏复核过。\n"
            "       能出片、能过字形检测，但不建议直接交付；节奏校准见 "
            "references/portrait-and-motion.md\n")

    if a.batch:
        # 批量只吃清单里的 per-film 字段；这些开关在批量下要么没意义、要么会被静默丢掉。
        # 以前它们全部无声失效：`--batch x.json --preview 15` 会真的跑全量成片（最贵的一条路），
        # 用户以为在出预览；`--layout portrait` 曾经父进程竖屏、子进程横屏。宁可早报错。
        _clash = [flag for flag, on in (("--preview", bool(a.preview)),
                                        ("--stage", a.stage not in ("", "all")),
                                        ("--gate", bool(a.gate)),
                                        ("--run-id", bool(a.run_id)),
                                        ("--vmaf", bool(a.vmaf)),
                                        ("--no-preflight", bool(a.no_preflight))) if on]
        if _clash:
            raise SystemExit("[batch] 与 %s 互斥：批量只按清单逐条跑 `--stage all`，"
                             "这些开关写进清单或用单条 render.py 指定"
                             % " / ".join(dict.fromkeys(_clash)))
        _setup_stdio()
        return run_batch(a)

    # work/ 本来就不存在 = 这一跑自己产出全部中间物，别把"我还没跑到"当成"产物过时"。
    work_pre_exists = os.path.isdir(a.work)
    try:
        os.makedirs(a.work, exist_ok=True)
    except OSError as e:
        raise SystemExit("[工作目录不可用] %s：%s（--work 指到一个能写的目录）" % (a.work, e)) from e
    script = load_script(a.script)
    engine = _engine(script, a.engine or None)
    if engine not in ("pil", "web"):
        raise SystemExit("engine 只能是 pil 或 web，收到 %r" % engine)
    script["_engine"] = engine
    script["_scenes"] = _resolve_scenes(a.scenes, a.script)

    # ---- 门禁账本 ----
    global _LEDGER
    # engine=web 时把入口页实际引用的同目录页面脚本一起纳入内容指纹：web 的画面逻辑在
    # web/*.js 里，不在 scenes.py 里——不纳入的话改动画后旧签核仍然"有效"，
    # 正好违背「签核绑内容指纹，改了画面代码旧签核自动失效」这条承诺。
    web_entry = ""
    if engine == "web":
        try:
            web_entry = _web_entry(script)   # 入口页不存在时这里会 SystemExit（本来就该早失败）
        except SystemExit:
            web_entry = ""                   # 页面还没建：交给后面的 stage 报错，账本先照建
    _LEDGER = GT.GateLedger(a.work, script, scenes_path=script["_scenes"],
                            script_path=a.script, engine=engine,
                            web_entry=web_entry, run_id=a.run_id)
    if a.no_gate:
        _LEDGER.bypass("人工签核检查（--no-gate）", "本条不算通过，只记录")
        print("[gate] --no-gate：跳过人工签核检查（会在 QC 报告里留痕）")

    def _sources():
        """源头输入：剧本、scenes.py、冻结设计（改它们等于改了所有下游的依据）。

        design.json 是第三个源头：它由 propose.py --confirm 写出，
        记录"这一版长什么样"。改设计 = 画面必须重渲，所以它必须进 stagewatch 的
        源头指纹，否则"改了配色但没重跑 frames"这种事故查不出来。
        """
        design = os.path.join(a.work, "design.json")
        return {"script": a.script, "scenes": script.get("_scenes") or "",
                "design": design if os.path.exists(design) else ""}

    # 本次命令行**计划要跑**的 stage 集合：--stage all 展开成 10 个，单 stage 就是它自己。
    # 用途见 stagewatch.check 的 pending：本阶段马上要用新输入重算，那种"输入变了"不是过时。
    _ALL_ORDER = ("style", "tts", "layout", "cues", "score", "sfx", "audio",
                  "frames", "mux", "qa")

    def _planned(stage_name):
        if stage_name == "all":
            return list(_ALL_ORDER)
        return [stage_name] if stage_name in STAGES else []

    def _stage_begin(name):
        """stage 开始时查新鲜度并计时。返回 t0。

        report() 自己负责打印 "[stale] <stage>: <原因>" 与"最小重跑"两行
        （必须接它的返回值，否则用户只看到"最小重跑"不知道是哪个阶段、
        为什么过时，得上 --stage explain 才知道）。
        --allow-stale 也要调用：它承诺的是"放行并留痕"（QC 报告里记一条 skipped），
        直接跳过检查连痕都没有——那就成了静默。
        """
        if name:
            # 本阶段本轮一定真的会跑，所以必须进 pending：`--stage tts` 会顺带排轴、
            # `--stage stills` 会自动补 tts/layout，这些"顺手跑的下游"不在 _planned(stage)
            # 里；不加的话 --strict-stale 下刚写完的 tts.json 会把 layout 判成
            # "输入变了却还要用旧产物"，把自动补前置这条路径整个堵死。
            pending = list(_planned(stage))
            if name not in pending:
                pending.append(name)
            errs, _warns = SW.report(a.work, [name], strict=a.strict_stale,
                                     allow_stale=a.allow_stale,
                                     ledger=_LEDGER, sources=_sources(),
                                     self_built=not work_pre_exists,
                                     pending=pending)
            if errs:
                # --strict-stale 的承诺是"上游产物过时算失败"。stagewatch.report 只把
                # 问题放进返回值（并往账本记一条 fail），它自己不抛异常——所以必须在这里
                # 真的失败。丢弃返回值的话，--stage layout --strict-stale 报完过时
                # 照跑、退出码 0，help 文本与行为对不上。
                #
                # 选择"立刻退出"而不是"跑完再在收尾处报"：过时的输入会把旧产物掺进
                # 新结果（比如拿旧 cues.json 渲新时间轴的画面），跑完再报既已浪费渲染
                # 时间，产出的东西也不能用。--stage all 同理：一发现就停。
                # 不写 work/gates.json：那是**交付账本**（上一轮 --stage all 的完整
                # 结论）。中间阶段一次失败就把它覆盖成单条 fail，用户手里那份
                # QC-REPORT.md 会变成「未判定」，比留证据代价更大。失败证据在
                # stdout 与退出码里，够定位。
                # 原因行 report() 已经逐条打过了，这里只补"为什么停下 + 怎么办"，
                # 不再把 errs 原样贴一遍。
                raise SystemExit(
                    "[stale][strict] 上面 %d 处上游产物过时 -> 本阶段不跑，直接失败。\n"
                    "  （--strict-stale 下过时=失败；继续跑会把旧产物掺进新结果，"
                    "白渲一遍还是错的。）\n"
                    "  怎么办：\n"
                    "    1) 照着每条下面的『最小重跑』先补上游，再重跑当前阶段\n"
                    "    2) 确认旧产物可以接受：加 --allow-stale 显式放行"
                    "（退出码 0，QC 报告里记 skipped）\n"
                    "    3) 只想看现状不跑：python render.py --stage explain"
                    % len(errs))
        return time.time()

    def _stage_end(name, t0, **extra):
        """stage 结束时落盘：manifest 快照 + 耗时台账。"""
        if not name:
            return
        try:
            SW.snapshot(a.work, name, _sources())
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] manifest 快照失败:", e)
        wall = time.time() - t0
        if a.cost:
            try:
                GT.append_cost(a.work, name, wall, **extra)
            except Exception as e:                          # noqa: BLE001
                print("   [WARN] cost.jsonl 写入失败:", e)
        print("   [time] %s 用时 %.1fs%s" % (name, wall, "（已记 work/cost.jsonl）" if a.cost else ""))

    def _check_signoff(where):
        """交付前的签核检查：缺签只告警，--strict-gates 下算失败。"""
        if a.no_gate:
            return
        state, stale = GT.signoff_state(a.work, _LEDGER.content_hash)
        bad = [g for g, v in state.items() if v["status"] == GT.SKIP]
        rejected = [g for g, v in state.items() if v["status"] == GT.FAIL]
        for g in rejected:
            _LEDGER.fail("signoff:" + g, "人工签核：" + GT.GATES[g], state[g]["detail"])
        if bad:
            _LEDGER.skip("signoff", "人工签核（--stage accept 签核）",
                         "未签/已失效：%s%s"
                         % ("、".join(bad), ("（内容已变：%s）" % "、".join(stale)) if stale else ""))
            print("   [WARN] 人工签核未完成：%s" % "、".join(bad))
            print("          交付前请 --stage accept --gate <名字>；"
                  "--strict-gates 会把它算作失败")
            for g in bad:
                print("              %-8s %s" % (g, GT.GATES[g]))
        elif not rejected:
            _LEDGER.ok("signoff", "人工签核", "%d/4 道闸已签" % len(GT.GATE_ORDER))
    if a.force_music:
        plan, _why = K.music_plan(script, has_music_file=os.path.exists(os.path.join(a.work, "music.wav")))
        if plan in ("off", "bed", "file"):
            print("[force-music] %s：不清 work/music.wav（那是外部配乐 / 本片不用配乐）" % plan)
        else:
            for name in ("music.wav", "score.json"):
                p = os.path.join(a.work, name)
                if os.path.exists(p):
                    os.remove(p)
    tl_p = os.path.join(a.work, "timeline.json")
    tts_p = os.path.join(a.work, "tts.json")

    def ensure_tl():
        if not os.path.exists(tl_p):
            raise SystemExit("[前置] 没有 %s，先跑 --stage layout" % tl_p)
        try:
            return json.load(open(tl_p, encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit("[前置] %s 不是合法 JSON（%s），重新跑 --stage layout" % (tl_p, e)) from e

    def load_scenes(strict=True):
        """加载场景模块：strict=False 时失败只 WARN（qa 缺参考帧也要能跑）。

        路径用 main() 里解析好的 script["_scenes"]（显式 --scenes / 剧本同目录 /
        技能内置三档回退），不能用原始 a.scenes —— 缺省时它是空串，会变成
        `load_module("")` 这种 "NoneType has no attribute loader" 的怪错误。
        """
        scenes_path = script.get("_scenes") or _resolve_scenes(a.scenes, a.script)
        try:
            mod = load_module(scenes_path)
        except Exception as e:                              # noqa: BLE001
            if strict:
                raise SystemExit("[场景模块加载失败] %s：%s\n"
                                 "  --scenes 要指向定义 draw_frame() 的 scenes.py"
                                 % (scenes_path, e)) from e
            print("   [WARN] 场景模块加载失败（%s），跳过场景函数参考帧: %s" % (scenes_path, e))
            return None
        if not hasattr(mod, "draw_frame"):
            if strict:
                raise SystemExit("[场景模块错误] %s 没有 draw_frame(scene, t_local, dur, t_global, script, frame)"
                                 % scenes_path)
            print("   [WARN] %s 没有 draw_frame()，跳过场景函数参考帧" % scenes_path)
            return None
        return mod

    stage = a.stage

    # ---- preview：前 N 秒试片 ----
    if a.preview:
        try:
            secs = float(a.preview)
        except ValueError:
            raise SystemExit("[preview] 需要秒数，例如 --preview 15") from None
        if secs <= 0:
            raise SystemExit("[preview] 秒数必须 > 0")
        # 自动补前置（照抄 stills 分支的写法）：README 把 --preview 15 列在常用开关里，
        # 可干净 work/ 下直接 ensure_tl() 会 SystemExit「先跑 --stage layout」——照文档
        # 抄一遍必失败，而且用户看不出"只差一次 TTS + 排轴"。这里缺什么就真跑什么，
        # 每一步都**明说**补的是哪一步（不偷偷跑）。
        # 为什么每步都要 _stage_begin/_stage_end：否则 manifest 里没有 tts/layout 记录，
        # 下次 --stage tts 会看到 tts.json 在、账本里却没记录 -> 误报「接手旧 work」。
        if not os.path.exists(tts_p):
            print("[preview] 没有 tts.json，自动先跑 --stage tts（需要联网合成旁白）")
            _t0 = _stage_begin("tts")
            lines = st_tts(script, a.work, force=a.force_tts)
            _stage_end("tts", _t0, n_tts=len(lines) if isinstance(lines, list) else None)
            _t0 = _stage_begin("layout")
            tl = st_layout(script, a.work, lines)
            _stage_end("layout", _t0)
        elif not os.path.exists(tl_p):
            print("[preview] 没有 timeline.json，自动先跑 --stage layout")
            _t0 = _stage_begin("layout")
            tl = st_layout(script, a.work)
            _stage_end("layout", _t0)
        else:
            tl = ensure_tl()
        mod = load_scenes()
        pdir = os.path.dirname(os.path.abspath(a.out))
        os.makedirs(pdir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(a.out))[0]
        out_mp4 = os.path.join(pdir, "preview", stem + "_preview.mp4")
        os.makedirs(os.path.dirname(out_mp4), exist_ok=True)
        n = min(int(round(secs * float(tl["fps"]))), int(tl["frames"]))
        print("== Preview（前 %.1fs / %d 帧）" % (n / float(tl["fps"]), n))
        sc2 = dict(script)
        sc2["_total"] = tl["total"]
        # 不能写成 setdefault(_style, resolve_style(script))：
        # 对 sc2 做 setdefault 却传 script 的解析结果 —— script 有 _style 时，
        # setdefault 的第二参数**仍会被求值**（Python 先算参数），等于多做一次无用解析；
        # 而 script 没有 _style 时又把 work 漏了，单跑 --preview 会报"找不到设计"。
        sc2.setdefault("_style", resolve_style(script, a.work))
        _render_range(tl, mod, sc2, 0, n, out_mp4, int(script.get("crf", 18)),
                      str(script.get("preset", "medium")), progress=False)
        # 有 audio.wav 就带上声音（没有也能看画面；不阻断）。
        # 注意：**必须先把音轨截到预览时长**——K.mux 会校验音视频时长差 < 0.05s，
        # 直接拿全长 audio.wav 去配 8 秒画面必然被判"音视频时长不一致"（实测）。
        audio = os.path.join(a.work, "audio.wav")
        if os.path.exists(audio):
            tmp = out_mp4 + ".mux.mp4"
            try:
                pcm, _sr = K.wav_read(audio)
                need = int(round(n / float(tl["fps"]) * float(_sr)))
                if need > 0:
                    pcm = pcm[:need]
                p_audio = os.path.join(a.work, "preview_audio.wav")
                K.wav_write(p_audio, pcm)
                audio = p_audio
                K.mux(out_mp4, audio, tmp, loudnorm=False)
                os.replace(tmp, out_mp4)
            except Exception as e:                          # noqa: BLE001
                print("   [WARN] 预览片混音失败（保留无声版）：%s" % e)
        gif = os.path.splitext(out_mp4)[0] + ".gif"
        try:
            _to_gif(out_mp4, gif, fps=min(10, int(tl["fps"])), width=480)
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] GIF 生成失败（mp4 仍然可用）：%s" % e)
        print("   预览片:", out_mp4)
        if os.path.exists(gif):
            print("   GIF  :", gif)
        return 0

    # ---- explain：只报"哪些阶段过时了"，不跑任何 stage ----
    if stage == "explain":
        rows = SW.explain(a.work, sources=_sources())
        bad = 0
        for r in rows:
            if r["issues"]:
                bad += 1
            print("%s%-8s %s" % ("!! " if r["issues"] else "OK ", r["stage"],
                                 r["at"] or "（无记录）"))
            for it in r["issues"]:
                print("     -", it["reason"])
                if it.get("rerun"):
                    print("       最小重跑:", it["rerun"])
        print()
        if bad:
            print("%d 个阶段过时。默认只告警；--strict-stale 让它失败。" % bad)
            return 1 if a.strict_stale else 0
        print("没有发现过时的阶段（按 work/manifest.json 判断）。")
        return 0

    # ---- accept：人工签核一道闸（写 work/signoff.json，绑内容指纹）----
    if stage == "accept":
        if not a.gate:
            raise SystemExit("[accept] 必须指定要签哪一道闸：--gate %s"
                             % " / ".join(GT.GATE_ORDER))
        if a.verdict == "approved":
            # 签核前必须能证明"真的看过了"：证据产物不在就拒绝签（不然签核是橡皮图章）。
            # 证据可能在 work/（渲染产物），也可能在项目根（FACTS.md 是人写的输入），两处都找。
            proj = os.path.dirname(os.path.abspath(a.script))
            want = {"style": [os.path.join(a.work, "style.json"),
                              os.path.join(a.work, "voice_plan.json")],
                    "stills": [os.path.join(a.work, "qa", "sheet.png"),
                               os.path.join(a.work, "stills", "_sheet.png")],
                    "facts": [os.path.join(proj, "FACTS.md"), os.path.join(a.work, "FACTS.md")],
                    "release": [a.out]}.get(a.gate, [])
            # 每一组是"多选一"：任一条路径存在就算有证据。
            missing = None
            if want and not any(os.path.exists(str(w)) for w in want):
                missing = "、".join(os.path.relpath(w, proj) if os.path.isabs(w) else w
                                   for w in want)
            if missing:
                raise SystemExit("[accept] 证据不存在，拒绝签核：先跑出要看的东西（缺 %s）"
                                 % missing)
        rec = GT.write_signoff(a.work, a.gate, _LEDGER.content_hash,
                               verdict=a.verdict, by=a.by or "user",
                               note=a.note,
                               artifacts=[os.path.join(a.work, "gates.json")])
        print("[accept] %s = %s（内容指纹 %s，%s）"
              % (a.gate, rec["verdict"], _LEDGER.content_hash, rec["atText"]))
        if a.verdict == "rejected":
            print("[accept] 已记「否决」（不算签核通过）；改了内容后需重新签。")
        # accept 是**独立进程**：本次账本是空的。直接 write_json 会把上一次 all/mux/qa
        # 记的 8 条门禁结论整个覆盖掉（报告变成 "pass 0 · skipped 1"，交付凭据被打穿）。
        # 先把磁盘上的旧账本搬回来（内容指纹一致才搬），再追加签核状态。
        _reload_ledger_from_disk(a.work, _LEDGER)
        # 签核状态进账本：否则报告里的"人工签核"一行仍是上一次 all/mux 收尾时记的
        # skipped，用户签完再打开报告却看不到"已签"——签核就不算兑现。
        try:
            _check_signoff(stage)
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] 签核状态没记进账本:", e)
        try:
            _LEDGER.write_json()
        except Exception as e:                              # noqa: BLE001
            print("   [WARN] gates.json 写入失败:", e)
        p_rep = GT.write_report(a.work, _report_path(a))
        print("[accept] QC 报告已更新:", p_rep)
        return 0

    if stage in ("all", "style"):
        # --stage all 也要把 style 这一行记进 manifest：若 _nm 在 all 下是 None，
        # 而 _stage_begin/_stage_end 对 None 是空操作 -> work/style.json 写出来了、
        # manifest.json 却从来没有 style 记录。于是之后任何一次 `--stage style` 都会
        # 命中「产物在但没有记录」的判据，报成 no-manifest（--stage all 的用户 100% 踩到）。
        _t0 = _stage_begin("style")
        resolve_style(script, a.work)
        _stage_end("style", _t0, n_tts=None)
        if stage == "style":
            print("== Style")
            _print_style(script["_style"])
            _need(os.path.exists(os.path.join(a.work, "style.json")), "style.json 没写出来")
            _LEDGER.ok("style", "风格包解析", str(script["_style"].get("style")))
            _LEDGER.write_json()
            # 不能无条件 return 0：--strict-gates 下 skipped 会让 _finish_gates 返回 1，
            # 账本里也可能已经有 fail —— 门禁结论必须落到退出码，否则 CI 把失败当成功。
            rc = _finish_gates(a)
            return 1 if (rc or _LEDGER.fails()) else 0

    if stage in ("all", "tts"):
        _t0 = _stage_begin("tts")
        lines = st_tts(script, a.work, force=a.force_tts)
        _stage_end("tts", _t0, n_tts=len(lines) if isinstance(lines, list) else None)
        # 既有行为：单跑 tts 也会排一次轴（tts.json 的时长信息要用），这里一并计时。
        _t0 = _stage_begin("layout")
        tl = st_layout(script, a.work, lines)
        _stage_end("layout", _t0)
    if stage == "layout":
        _t0 = _stage_begin("layout")
        _need(os.path.exists(tts_p), "没有 tts.json，先跑 --stage tts")
        tl = st_layout(script, a.work)
        _stage_end("layout", _t0)
    if stage in ("all", "cues"):
        tl = tl if stage == "all" else ensure_tl()
        _t0 = _stage_begin("cues")
        cues = st_cues(script, a.work, tl, engine=engine)
        _stage_end("cues", _t0,
                   n_cues=(len((cues or {}).get("cues") or []) if isinstance(cues, dict) else None))
    if stage in ("all", "score"):
        tl = tl if stage == "all" else ensure_tl()
        _t0 = _stage_begin("score")
        st_score(script, a.work, tl)
        _stage_end("score", _t0)
    if stage in ("all", "sfx"):
        tl = tl if stage == "all" else ensure_tl()
        _t0 = _stage_begin("sfx")
        st_sfx(script, a.work, tl)
        _stage_end("sfx", _t0)
    if stage in ("all", "audio"):
        tl = tl if stage == "all" else ensure_tl()
        _need(all(s.get("path") for s in tl["scenes"]), "timeline 里没有旁白路径，先跑 --stage tts")
        missing = [s.get("id") for s in tl["scenes"] if not os.path.exists(str(s.get("path") or ""))]
        _need(not missing, "旁白文件不存在：%s（tts 目录被删/挪过？重新跑 --stage tts）"
              % (missing[:6],))
        _t0 = _stage_begin("audio")
        st_audio(tl, a.work, script)
        _stage_end("audio", _t0)

    if stage == "stills":
        # 母版预览是"上手第一件事"，可它需要 timeline。干净目录直接 SystemExit 的话，
        # 文档三步上手就把它排在第 4 步（照抄必失败）。所以这里自动补前置：
        # 缺 tts.json 就真合成一次旁白（要联网），缺 timeline 就排一次轴——
        # 但每一步都**明说**自己在补什么，不偷偷跑。
        # 自动补的这几步也要 _stage_begin/_stage_end：否则 manifest 里只有 stills 一条记录，
        # 下次 --stage tts 会看到 tts.json 在、账本里没有 tts 记录 -> 误报「接手旧 work」。
        if not os.path.exists(tts_p):
            print("[stills] 没有 tts.json，自动先跑 --stage tts（需要联网合成旁白）")
            _t0 = _stage_begin("tts")
            lines = st_tts(script, a.work, force=a.force_tts)
            _stage_end("tts", _t0, n_tts=len(lines) if isinstance(lines, list) else None)
            _t0 = _stage_begin("layout")
            tl_tmp = st_layout(script, a.work, lines)
            _stage_end("layout", _t0)
            tl = tl_tmp
        elif not os.path.exists(tl_p):
            print("[stills] 没有 timeline.json，自动先跑 --stage layout")
            _t0 = _stage_begin("layout")
            tl = st_layout(script, a.work)
            _stage_end("layout", _t0)
        else:
            tl = ensure_tl()
        mod = load_scenes()
        specs = [x.strip() for x in a.stills.split(",") if x.strip()] if a.stills else None
        _t0 = _stage_begin("stills")
        made = st_stills(tl, a.work, mod, script, specs)
        _stage_end("stills", _t0)
        _LEDGER.add("stills", "母版静帧已产出", GT.OK if made else GT.FAIL,
                    "%d 张（要人看过才算过：--stage accept --gate stills）" % len(made or []))
        # 0 张静帧 = fail 门禁：无条件 return 0 的话，`--stills 写错 id` 会
        # "全部跳过 + 退出码 0"，CI 看不出根本没出图。
        rc = _finish_gates(a)
        return 1 if (rc or _LEDGER.fails()) else 0

    if stage == "qa":
        # qa 不重渲、也不重新合成：缺成片就直接报错；缺 scenes.py 只丢参考帧、不阻断质检。
        tl = ensure_tl()
        # mux 的产出路径由 --out 决定，qa 读的是同一个 --out：提示必须把它写出来，
        # 否则「out/portrait.mp4 明明在，qa 却说没有 out/film.mp4」，人只能靠猜。
        _need(os.path.exists(a.out),
              "没有成片 %s，先跑 --stage mux --out %s（qa 读的就是同一个 --out，两条命令的 --out 必须一致）"
              % (a.out, a.out))
        mod = load_scenes(strict=False) if engine == "pil" else None
        _t0 = _stage_begin("qa")
        errs = st_qa(a.out, tl, a.work, scenes_mod=mod, script=script, vmaf_mode=a.vmaf)
        _stage_end("qa", _t0)
        _check_signoff("qa")
        rc = _finish_gates(a)
        return 1 if (errs or rc) else 0

    if stage in ("all", "frames", "mux"):
        # ---- 提案闸：出片前必须先有已确认的设计 ----
        # 放在最前面（before ensure_tl/preflight/st_frames）：渲完再拦等于白渲。
        if stage in ("all", "frames"):
            if not _proposal_gate(a):
                return 1
        tl = tl if stage == "all" else ensure_tl()
        audio = os.path.join(a.work, "audio.wav")
        silent = os.path.join(a.work, "silent.mp4")
        mod = None
        if stage in ("all", "frames"):
            # 渲染帧本身不需要音频（silent.mp4 是无声的）。只有 --stage all 才要求
            # audio.wav 已经在前面阶段生成——否则 --stage frames 在空 work 目录会白渲全片再挂掉。
            if stage == "all":
                _need(os.path.exists(audio), "混音阶段异常：audio.wav 未生成")
            if not _run_preflight(script, a.work, engine, tl=tl,
                                  skip=bool(a.no_preflight)):
                print("\n[渲染前预检未过] 按上面逐条修；确认要硬跑加 --no-preflight（会留痕）")
                return 1
            if engine == "pil":
                mod = load_scenes()
            _t0 = _stage_begin("frames")
            silent = st_frames(tl, a.work, mod, script, jobs=a.jobs, resume=a.resume,
                               engine=engine)
            _stage_end("frames", _t0, frames=int(tl["frames"]))
            if stage == "frames":
                _LEDGER.ok("frames", "逐帧渲染", "%d 帧（%s）" % (int(tl["frames"]), engine))
                # 同上：strict-gates 下 skipped 也要反映到退出码（只看 fails() 会漏掉它）。
                rc = _finish_gates(a)
                return 1 if (rc or _LEDGER.fails()) else 0
        if stage in ("all", "mux"):
            # mux 只需要 silent.mp4 + audio.wav：不加载 scenes.py（缺场景模块也能合成）
            _need(os.path.exists(silent), "没有 silent.mp4，先跑 --stage frames")
            _need(os.path.exists(audio), "没有 audio.wav，先跑 --stage audio")
            _t0 = _stage_begin("mux")
            film = st_mux(silent, audio, a.out, script)
            _stage_end("mux", _t0)
            _check_signoff("mux")
            if stage == "mux":
                _LEDGER.ok("mux", "合成（视频流 copy + loudnorm）", a.out)
                # 同上：strict-gates 下 skipped 也要反映到退出码（只看 fails() 会漏掉它）。
                rc = _finish_gates(a)
                return 1 if (rc or _LEDGER.fails()) else 0
            if stage == "all":
                _t0 = _stage_begin("qa")
                errs = st_qa(film, tl, a.work, scenes_mod=mod, script=script,
                             vmaf_mode=a.vmaf)
                _stage_end("qa", _t0)
                rc = _finish_gates(a)
                if errs or rc:
                    print("\n[质检未过] 成片已生成但没有通过门禁，按上面 [FAIL] 逐条修。")
                    return 1
                return 0
    # 所有 stage 统一收尾：落 gates.json + QC-REPORT.md，并按门禁结果给退出码。
    # 只有 style/frames/mux/qa 这几条分支自己调 _finish_gates 时，其余
    # （tts/layout/cues/score/sfx/audio/stills）会落在这行 return 0——于是 ledger 里
    # 记的 fail 传不到退出码，"门禁失败 -> 非 0"这个承诺按 stage 不同行为不一样。
    # 默认路径不受影响：没有 fail（且没开 --strict-gates）时 _finish_gates 返回 0。
    rc = _finish_gates(a, persist=(stage in _PERSIST_STAGES))
    return 1 if (rc or _LEDGER.fails()) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C：给「能不能续跑」的明确答复，而不是甩 traceback 或静默退出 130。
        # 分块渲染先写 _part_ 再改名，所以中断不会留下被 --resume 误认的半截分块；
        # 已完成的段下次加 --resume 直接复用，残留的 _part_ 会被自动清掉。
        _setup_stdio()
        print("\n[中断] 收到 Ctrl-C，本次运行已停止。", file=sys.stderr)
        print("  已完成的产物都留在 work/ 里：分块渲染请加 --resume 续跑"
              "（半成品段不会被当成完成段复用）。", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        # 领域错误（音视频时长不符 / 分块缺口 / 编码失败 / TTS 重试耗尽 …）给一句话，
        # 不要甩 Python traceback；要完整栈就 set RENDER_DEBUG=1。
        if os.environ.get("RENDER_DEBUG"):
            raise
        _setup_stdio()
        print("\n[ERROR] %s" % e, file=sys.stderr)
        print("  （要看完整 traceback：set RENDER_DEBUG=1 后重跑）", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        if os.environ.get("RENDER_DEBUG"):
            raise
        _setup_stdio()
        print("\n[ERROR] 文件/目录操作失败：%s" % e, file=sys.stderr)
        print("  （要看完整 traceback：set RENDER_DEBUG=1 后重跑）", file=sys.stderr)
        sys.exit(1)
