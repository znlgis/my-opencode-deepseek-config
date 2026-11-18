# -*- coding: utf-8 -*-
"""preflight.py — 渲染前资源闸门。

为什么要有它：缺字形（✓ / emoji / 生僻字）和坏 TTS 音频都是「渲染完才报」的问题——
filmkit.glyph_missing_chars() 检测本身是准的（正常中文 []，✓/↵/emoji 检出），
但它现在只在 layout / qa 阶段被调用：等 300 帧渲完，才发现某句 big 里有个方块。
preflight 不新写检测器，只把现成检测前移，并把结论整理成「字符 + 字段 + 句 id」的结构化错误：
**渲染前一秒就能 fail，而不是渲完才 fail**。

检查项（三档语义 pass / fail / skipped，**"没查"绝不能写成"通过"**）：
  1 script      剧本可解析、最小结构合法（title/lines/id 唯一/text 非空）
  2 glyphs      所有上屏文本逐字过 filmkit.glyph_missing_chars；
                字段：text / sub / big / kicker / lead / note / title / end_text /
                chapter / items[].text（含 timeline.scenes 里的同名字段）
                按夹具实际绘制权重检测：big/kicker=bold、lead=light、sub=reg+light、
                items=reg、title/chapter=reg、text=bold+reg
  3 tts         work/tts/*.mp3 若存在，必须能被 ffprobe 解析出音频流；
                文件名要能对上行 id，时长要与台词长度相符（过短=截断/坏缓存）
  4 fonts       字体候选至少一个真的能加载（filmkit.font() 对 bold/reg/light 各取一次）
  5 web_page    engine="web" 时入口页存在（script.web.entry 或 <剧本目录>/web/index.html）

接口（纯函数，不打印、不落盘、不改入参）：
    check(script, *, work=None, engine="pil", timeline=None) -> dict
      script   : film.json 的 dict，或 film.json 路径（str/Path）
      work     : work 目录（有 work/tts/*.mp3 才查音频；None = 不查，记 skipped）
      engine   : "pil"（默认）| "web"
      timeline : work/timeline.json 的 dict 或路径（可选，用于扫场景上的字幕文本、
                 以及提示"timeline 已过时"）
    返回 {"ok": bool, "errors": [{code,msg,where,...}], "warnings": [...],
          "checks": {检查项: {"status": "pass|fail|skipped", "detail": str}}, "engine": str}

CLI：
    python preflight.py --script film.json [--work work] [--engine pil]
                        [--timeline work/timeline.json] [--json] [--strict]
    退出码：0 = 没有 error；1 = 有 error（--strict 时 skipped 也算 error）。
    --json 时 stdout 只有 JSON（键名全英文），给人看的提示走 stderr。

依赖：仅标准库；glyph/font 检查需要同目录的 filmkit.py + Pillow（缺了就是 error，不静默跳过）。
ffmpeg/ffprobe 探测口径与 filmkit 一致：FILMKIT_FFMPEG 优先，找不到可执行文件一律 fail-closed。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _early_layout(argv=None):
    """在 import filmkit/previewkit **之前**把 --layout 落到环境变量。

    为什么要在模块顶部做：scenes.py 写的是 `from filmkit import W, H`，而 filmkit 的 W/H
    来自 layoutkit —— 值在导入那一刻就绑死了。等 argparse 解析完再设环境变量，此时画布
    早就定了，`--layout portrait` 会静默失效（最坏的那种 bug：以为在出竖屏，实际是横屏）。
    与 render.py 同一条纪律（那边是为了 `--stage frames`，这边是为了提案/校验）。

    只认 `--layout X` 与 `--layout=X` 两种写法；别的参数一概不碰。
    """
    import os as _os, sys as _sys
    args = list(_sys.argv[1:] if argv is None else argv)
    want = ""
    for i, tok in enumerate(args):
        if tok == "--layout" and i + 1 < len(args):
            want = args[i + 1]
            break
        if tok.startswith("--layout="):
            want = tok.split("=", 1)[1]
            break
    if want:
        _os.environ["VIDEO_STUDIO_LAYOUT"] = want


_early_layout()

if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                                    # 同目录的 filmkit 提供字形/字体/ffprobe 口径
    import filmkit as _K
except Exception:                       # noqa: BLE001 —— 独立拷走本文件时给出可读错误
    _K = None

# 检查项 id（checks 里的键，顺序即打印顺序）
CHECK_IDS = ("script", "glyphs", "tts", "fonts", "web_page")

# 上屏文本字段 -> 该字段实际被绘制的字重（见 assets/scenes.py 的绘制函数）。
# 为什么要分权重：msyhbd / msyh / msyhl 是三个文件，字形覆盖不完全相同；
# 用错权重的检测会把"只有 reg 缺、但该字段画 reg"误报成"没事"，或者反过来。
FIELD_WEIGHTS = {
    "text": ("bold", "reg"),        # body 缺 big 时取 text[:18] 画 bold；字幕回退画 reg
    "sub": ("reg", "light"),        # body 画 reg；hook / outro 画 light
    "big": ("bold",),
    "kicker": ("bold",),
    "lead": ("light",),
    "note": ("bold",),              # 夹具未用；按 QA 的 check_glyphs 口径（bold）兜底
    "title": ("reg", "bold"),       # 右下角 brand 画 reg；hook 缺 big 时画 bold
    "end_text": ("bold",),
    "chapter": ("reg",),
}
ITEM_FIELD = "items[].text"
ITEM_WEIGHTS = ("reg",)
# QA 阶段沿用的口径（filmkit.check_glyphs 用 32/bold 扫 text/sub/big/lead/kicker/note），
# preflight 与它的差异只在"按字段选权重"，所以两者冲突时以本文件更细的口径为准。

# 探测单个 mp3 的超时（秒）；坏文件/网络盘上 ffprobe 也可能卡住
PROBE_TIMEOUT = 15


# ---------------------------------------------------------------- 小工具
def _setup_stdio():
    """输出被重定向时切 UTF-8（Windows 控制台默认 GBK，中文/符号会乱码）。

    与 render.py / selfcheck.py 同口径：只在非交互（管道/日志）时切编码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:               # noqa: BLE001
            pass


def _err(errors, code, msg, where="", **extra):
    """记一条 error（code/msg/where 是契约，extra 里的 line_id/field/chars 便于机读）。"""
    item = {"code": str(code), "msg": str(msg), "where": str(where or "")}
    for k, v in extra.items():
        if v is not None:
            item[k] = v
    errors.append(item)
    return item


def _warn(warnings, code, msg, where="", **extra):
    item = {"code": str(code), "msg": str(msg), "where": str(where or "")}
    for k, v in extra.items():
        if v is not None:
            item[k] = v
    warnings.append(item)
    return item


def _check(checks, cid, status, detail=""):
    """记一项检查结论。status 只允许 pass/fail/skipped（三档语义，别名直接拒）。"""
    if status not in ("pass", "fail", "skipped"):
        raise ValueError("检查状态只能是 pass/fail/skipped，收到 %r" % (status,))
    checks[cid] = {"status": status, "detail": str(detail or "")}


# ---------------------------------------------------------------- 可执行文件探测
_FFMPEG_CACHE = []
_FFPROBE_CACHE = []


def _probe_exe(path):
    """候选是否真的可用：必须跑通 "<exe> -version" 且 returncode == 0（fail-closed）。

    只看 os.path.exists 是不够的：本机曾静默选中过 2019 年的旧 ffmpeg，一路跑到滤镜
    阶段才挂。探测阶段异常/超时一律视为不可用。
    """
    if not path:
        return ""
    exe = str(path)
    try:
        if not os.path.isfile(exe):
            exe = shutil.which(exe) or exe
            if not os.path.isfile(exe):
                return ""
        p = subprocess.run([exe, "-version"], capture_output=True, timeout=10)
        if p.returncode != 0:
            return ""
        return os.path.abspath(exe) if os.path.isabs(exe) else exe
    except Exception:                   # noqa: BLE001 —— 探测失败 = 候选不可用
        return ""


def _ffmpeg():
    """解析 ffmpeg：FILMKIT_FFMPEG 优先，其次 filmkit.FFMPEG，最后 PATH。找不到返回 ""。"""
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    cands = []
    env = (os.environ.get("FILMKIT_FFMPEG") or "").strip()
    if len(env) >= 2 and env[0] == env[-1] and env[0] in "\"'":
        env = env[1:-1].strip()         # 容忍用户把路径连引号一起粘进环境变量
    if env:
        cands.append(env)
    if _K is not None:
        try:
            cands.append(str(getattr(_K, "FFMPEG", "") or ""))
        except Exception:               # noqa: BLE001
            pass
    cands.append(shutil.which("ffmpeg") or "")
    for c in cands:
        hit = _probe_exe(c)
        if hit:
            _FFMPEG_CACHE.append(hit)
            return hit
    _FFMPEG_CACHE.append("")
    return ""


def _ffprobe():
    """解析 ffprobe：FFMPEG 同目录的 ffprobe(.exe) 优先，其次 filmkit.ffprobe_path()，最后 PATH。

    找不到返回 ""（调用方据此 fail-closed，而不是把"没查"写成"通过"）。
    """
    if _FFPROBE_CACHE:
        return _FFPROBE_CACHE[0]
    cands = []
    ff = _ffmpeg()
    if ff:
        base = os.path.basename(ff)
        names = ("ffprobe.exe", "ffprobe") if base.lower().endswith(".exe") \
            else ("ffprobe", "ffprobe.exe")
        d = os.path.dirname(os.path.abspath(ff))
        for n in names:
            cands.append(os.path.join(d, n))
    if _K is not None:
        try:
            cands.append(str(_K.ffprobe_path() or ""))
        except Exception:               # noqa: BLE001
            pass
    cands.append(shutil.which("ffprobe") or "")
    for c in cands:
        hit = _probe_exe(c)
        if hit:
            _FFPROBE_CACHE.append(hit)
            return hit
    _FFPROBE_CACHE.append("")
    return ""


def _ffprobe_json(path):
    """用 ffprobe 解析一个媒体文件，返回 (ok, 信息dict, 错误串)。"""
    exe = _ffprobe()
    if not exe:
        return False, {}, "找不到可用的 ffprobe（设置 FILMKIT_FFMPEG 指向 ffmpeg，或把 ffprobe 放进 PATH）"
    cmd = [exe, "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,duration,sample_rate,channels:format=duration,size",
           "-of", "json", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, {}, "ffprobe 超时（>%ds）" % PROBE_TIMEOUT
    except OSError as e:
        return False, {}, "ffprobe 启动失败：%s" % e
    if p.returncode != 0:
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return False, {}, "ffprobe 退出码 %d：%s" % (p.returncode, (tail[-1] if tail else "")[:300])
    try:
        data = json.loads((p.stdout or b"{}").decode("utf-8", "replace") or "{}")
    except ValueError as e:
        return False, {}, "ffprobe 输出不是合法 JSON：%s" % e
    return True, (data if isinstance(data, dict) else {}), ""


# ---------------------------------------------------------------- 剧本装载
def _load_script(script, errors):
    """script（dict 或路径）-> 规范化 dict；不修改调用方传入的对象。

    失败返回 None（错误已记进 errors）。字段默认值与 render.load_script 对齐：
    缺 id 自动补 l01、缺 sub 用 text（这样 preflight 扫的字段与真正上屏的一致）。
    """
    import copy
    if isinstance(script, dict):
        data = copy.deepcopy(script)
        path = str(data.get("_path") or "")
        base = str(data.get("_dir") or (os.path.dirname(os.path.abspath(path))
                                        if path else os.getcwd()))
    else:
        path = os.path.abspath(str(script))
        base = os.path.dirname(path)
        if not os.path.isfile(path):
            _err(errors, "script_missing", "剧本不存在：%s" % path, "script")
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:          # noqa: BLE001
            _err(errors, "script_json", "剧本不是合法 JSON：%s（%s）" % (path, e), "script")
            return None
    if not isinstance(data, dict):
        _err(errors, "script_shape", "剧本顶层必须是 JSON 对象（收到 %s）"
             % type(data).__name__, "script")
        return None
    data["_dir"] = base
    data["_path"] = path or str(data.get("_path") or "")
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        _err(errors, "script_shape", "剧本 lines 必须是非空数组（收到 %r）"
             % (type(lines).__name__,), "script.lines")
        return data
    seen = set()
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            _err(errors, "line_shape", "lines[%d] 必须是对象（收到 %s）"
                 % (i, type(ln).__name__), "lines[%d]" % i)
            continue
        ln.setdefault("id", "l%02d" % (i + 1))
        if "sub" not in ln and isinstance(ln.get("text"), str):
            ln["sub"] = ln["text"]
        text = ln.get("text")
        if not isinstance(text, str) or not text.strip():
            _err(errors, "line_text_missing", "lines[%d]（id=%s）缺少 text（朗读文本）"
                 % (i, ln.get("id")), "lines[%d].text" % i,
                 line_id=str(ln.get("id")), field="text")
        sid = str(ln.get("id"))
        if sid in seen:
            _err(errors, "line_id_dup", "lines[%d] 的 id=%r 重复（TTS 文件与 cue 会串）"
                 % (i, ln.get("id")), "lines[%d].id" % i, line_id=sid)
        seen.add(sid)
    if not str(data.get("title") or "").strip():
        _err(errors, "script_shape", "剧本缺少 title（右下角 brand 会画空）", "script.title")
    return data

# ---------------------------------------------------------------- 文本遍历
def _weights_for(field):
    return FIELD_WEIGHTS.get(field) or ("bold",)


def _iter_text_items(script):
    """产出所有上屏文本项：(where, line_id, field, text, weights)。

    覆盖剧本行字段 + items[].text + 剧本级 title/end_text。timeline 里的场景
    由 _iter_timeline_items 补扫（同一 id/字段不重复）。
    """
    lines = script.get("lines") if isinstance(script.get("lines"), list) else []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        sid = str(ln.get("id") or "l%02d" % (i + 1))
        for fld in ("text", "sub", "big", "kicker", "lead", "note", "chapter"):
            v = ln.get(fld)
            if isinstance(v, str) and v.strip():
                yield ("lines[%d].%s（id=%s）" % (i, fld, sid), sid, fld, v,
                       _weights_for(fld))
        items = ln.get("items")
        if isinstance(items, list):
            for j, it in enumerate(items):
                if isinstance(it, dict):
                    txt = it.get("text")
                else:
                    txt = it
                if isinstance(txt, str) and txt.strip():
                    yield ("lines[%d].items[%d].text（id=%s）" % (i, j, sid), sid,
                           ITEM_FIELD, txt, ITEM_WEIGHTS)
    for fld in ("title", "end_text"):
        v = script.get(fld)
        if isinstance(v, str) and v.strip():
            yield ("script.%s" % fld, "", fld, v, _weights_for(fld))


def _load_timeline(timeline, warnings):
    """timeline（dict 或路径）-> dict 或 None（可选输入；读不了只告警，不阻断）。"""
    if not timeline:
        return None
    if isinstance(timeline, dict):
        return timeline
    path = os.path.abspath(str(timeline))
    if not os.path.isfile(path):
        _warn(warnings, "timeline_missing", "timeline 不存在，跳过场景文本补扫：%s" % path,
              "timeline")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:              # noqa: BLE001
        _warn(warnings, "timeline_unreadable", "timeline 读不了，跳过场景文本补扫：%s（%s）"
              % (path, e), "timeline")
        return None
    if not isinstance(data, dict):
        _warn(warnings, "timeline_shape", "timeline 顶层不是对象，跳过场景文本补扫：%s" % path,
              "timeline")
        return None
    return data


def _iter_timeline_items(timeline, seen):
    """补扫 timeline.scenes 上的文本：layout 会拿 scenes 再查一遍字形。

    seen: {(line_id, field): text} 已扫过的剧本内容。同一个 (id, 字段) 且**文本相同**才跳过；
    文本不同说明 timeline 与剧本已经不一致（旧轴），照样查——漏扫等于把未知当通过。
    """
    scenes = (timeline or {}).get("scenes")
    if not isinstance(scenes, list):
        return
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("id") or "s%02d" % i)
        for fld in ("text", "sub", "big", "kicker", "lead", "note", "chapter"):
            v = sc.get(fld)
            if not isinstance(v, str) or not v.strip():
                continue
            if seen.get((sid, fld)) == v:
                continue
            yield ("timeline.scenes[%d].%s（id=%s）" % (i, fld, sid), sid, fld, v,
                   _weights_for(fld))


# ---------------------------------------------------------------- 检查：字形
def _check_glyphs(script, timeline, errors, warnings, checks):
    """所有上屏文本逐字过 filmkit.glyph_missing_chars（渲染前把方块字拦下来）。

    按字段实际字重检测（见 FIELD_WEIGHTS）：同一句 big 用 msyhbd 画、sub 用 msyh，
    三个字体文件覆盖不同，必须分开看。字号用 32（与 QA 的 check_glyphs 口径一致；
    字形覆盖只由字体文件决定，与字号无关）。
    """
    if _K is None:
        _err(errors, "filmkit_missing",
             "找不到同目录的 filmkit.py，无法做字形检测（fail-closed）", "glyphs")
        _check(checks, "glyphs", "fail", "filmkit.py 缺失")
        return
    raw_lines = script.get("lines")
    usable = [ln for ln in raw_lines if isinstance(ln, dict)] \
        if isinstance(raw_lines, list) else []
    if not usable:
        # 剧本结构坏到没有一行可扫：字形检查**没有执行**，只能 skipped（不能写 pass）
        _check(checks, "glyphs", "skipped", "剧本没有可扫的行（lines 不可用）")
        return
    n_texts = 0
    n_chars = 0
    bad_lines = 0
    items = list(_iter_text_items(script))
    seen = {(sid, fld): text for _where, sid, fld, text, _w in items if sid}
    items += list(_iter_timeline_items(timeline, seen))
    for where, sid, fld, text, weights in items:
        n_texts += 1
        n_chars += len(text)
        per_char = {}                   # char -> [weight...]
        for wgt in weights:
            try:
                miss = _K.glyph_missing_chars(text, 32, wgt)
            except Exception as e:      # noqa: BLE001 —— 字体加载失败已在 fonts 项报过
                _err(errors, "glyph_check_failed",
                     "字形检测执行失败（%s）：%s" % (type(e).__name__, e), where)
                _check(checks, "glyphs", "fail", "检测器异常")
                return
            for ch in miss:
                per_char.setdefault(ch, []).append(wgt)
        if per_char:
            bad_lines += 1
            chars = "".join(per_char.keys())
            wlist = sorted({w for ws in per_char.values() for w in ws})
            _err(errors, "glyph_missing",
                 "字段 %s 缺字形 %s（字重 %s）：渲染时会画成方块" %
                 (fld, " ".join(repr(c) for c in per_char.keys()), "/".join(wlist)),
                 where, line_id=sid, field=fld, chars=chars, weights=wlist)
    # 字族提示：filmkit.glyph_missing_chars 固定用 sans 字族的字体（msyh 一族）。
    # serif / kai / round 主题的实际字体文件不同，覆盖可能有差异——检测结论按 sans 口径给出，
    # 这里明确说一声，避免"preflight 过了但 serif 片子里是方块"的静默盲区。
    fam = _theme_family(script)
    if fam and fam != "sans":
        _warn(warnings, "glyph_family_unchecked",
              "本片风格字体族是 %r：字形检测按 sans（msyh）口径执行，%s 族字体覆盖未逐一核对"
              % (fam, fam), "glyphs")
    unscanned = len(raw_lines) - len(usable) if isinstance(raw_lines, list) else 0
    scope = "扫描 %d 段文本 / %d 字" % (n_texts, n_chars)
    if unscanned:
        scope += "（另有 %d 行结构异常未被扫，见 script 项错误）" % unscanned
    if bad_lines:
        _check(checks, "glyphs", "fail", "缺字形 %d 处（%s）" % (bad_lines, scope))
    elif unscanned:
        # 有行没扫 = 检测范围不完整，不能因为"扫到的部分没问题"就写 pass
        _check(checks, "glyphs", "skipped", scope + "，检测范围不完整")
    else:
        _check(checks, "glyphs", "pass", scope + "，无缺字形")


def _theme_family(script):
    """尽力取本片主题的字体族（sans/serif/kai/round）；取不到返回 ""。"""
    try:
        import stylekit as ST
    except Exception:                   # noqa: BLE001
        return ""
    try:
        plan = ST.resolve_script_style(script)
        th = plan.get("theme") if isinstance(plan, dict) else None
        return str((th or {}).get("family") or "")
    except Exception:                   # noqa: BLE001 —— 风格解析失败留给渲染阶段报错
        return ""


# ---------------------------------------------------------------- 检查：字体
def _check_fonts(errors, warnings, checks):
    """字体候选必须真的能加载（不只看文件存在）。"""
    if _K is None:
        _err(errors, "filmkit_missing",
             "找不到同目录的 filmkit.py，无法校验字体（fail-closed）", "fonts")
        _check(checks, "fonts", "fail", "filmkit.py 缺失")
        return
    cands = {}
    for wgt, paths in (getattr(_K, "FONT_CANDIDATES", {}) or {}).items():
        cands[wgt] = [p for p in (paths or []) if os.path.exists(p)]
    if cands and not any(cands.values()):
        _err(errors, "font_missing",
             "filmkit.FONT_CANDIDATES 里没有任何字体文件存在（渲染会抛「找不到中文字体」）",
             "fonts")
        _check(checks, "fonts", "fail", "0 个候选存在")
        return
    resolved, bad = {}, []
    for wgt in ("bold", "reg", "light"):
        try:
            f = _K.font(32, wgt)
            resolved[wgt] = os.path.basename(str(getattr(f, "path", "") or "?"))
        except Exception as e:          # noqa: BLE001
            bad.append("%s: %s" % (wgt, e))
    if bad:
        _err(errors, "font_unusable", "字体加载失败：%s" % "；".join(bad), "fonts")
        _check(checks, "fonts", "fail", "；".join(bad)[:160])
        return
    _check(checks, "fonts", "pass",
           "bold=%s / reg=%s / light=%s" % (resolved.get("bold"), resolved.get("reg"),
                                            resolved.get("light")))

# ---------------------------------------------------------------- 检查：TTS
def _check_tts(script, work, errors, warnings, checks):
    """work/tts/*.mp3 若存在，必须能被 ffprobe 解析出音频流。

    目录不存在 / work 没传 = skipped（"还没跑 TTS"是正常前置状态，不是通过）。
    目录存在但一个文件都没有 = skipped（同上）。有 mp3 但 ffprobe 不可用 = error（fail-closed）。

    除了"能解析出音频流"，还要**对得上剧本**，否则门禁只是看着干净：
      * 文件名与行 id 对账：filmkit.tts_batch 的文件名恒为 "<id>.mp3"，所以
        "某句没有对应 mp3 / 有剧本里没有的 mp3" 说明 work/tts 与剧本不是同一份
        （改过 id、拷来的旧缓存）。只比个数会漏掉"l09 顶替 l04"——数量相同但对不上。
      * 时长与台词长度对账：正常旁白约 0.15–0.25 s/字，连英文也不会快过 0.06 s/字。
        比这还短只可能是音频被截断 / 缓存坏了，渲染时这一句会被压没。
    这两项都是 warning（启发式，不阻断）：判据保守，但比静默放过强。
    """
    if not work:
        _check(checks, "tts", "skipped", "没传 work，未检查 TTS 音频")
        return
    tts_dir = os.path.join(str(work), "tts")
    if not os.path.isdir(tts_dir):
        _check(checks, "tts", "skipped", "还没跑 TTS（%s 不存在）" % tts_dir)
        return
    files = sorted(n for n in os.listdir(tts_dir) if n.lower().endswith(".mp3"))
    if not files:
        _check(checks, "tts", "skipped", "%s 里没有 .mp3" % tts_dir)
        return
    exe = _ffprobe()
    if not exe:
        _err(errors, "ffprobe_missing",
             "有 %d 个 TTS 音频需要校验，但找不到可用的 ffprobe：设置 FILMKIT_FFMPEG 指向 "
             "ffmpeg（同目录需有 ffprobe）或把 ffprobe 放进 PATH" % len(files), "tts")
        _check(checks, "tts", "fail", "ffprobe 不可用（fail-closed）")
        return
    bad = 0
    durs = {}
    for name in files:
        path = os.path.join(tts_dir, name)
        where = "tts/%s" % name
        try:
            size = os.path.getsize(path)
        except OSError as e:
            bad += 1
            _err(errors, "tts_unreadable", "TTS 音频读不了：%s（%s）" % (path, e), where)
            continue
        if size <= 0:
            bad += 1
            _err(errors, "tts_empty", "TTS 音频是空文件（0 字节）：%s" % path, where)
            continue
        ok, info, msg = _ffprobe_json(path)
        if not ok:
            bad += 1
            _err(errors, "tts_corrupt", "TTS 音频 ffprobe 解析失败（%s）：%s" % (path, msg), where)
            continue
        streams = [s for s in (info.get("streams") or [])
                   if str(s.get("codec_type")) == "audio"]
        if not streams:
            bad += 1
            _err(errors, "tts_no_audio", "TTS 文件里没有音频流：%s" % path, where)
            continue
        dur = None
        try:
            dur = float((info.get("format") or {}).get("duration") or 0.0) or None
        except (TypeError, ValueError):
            dur = None
        if dur:
            durs[name] = round(dur, 3)
        else:
            _warn(warnings, "tts_no_duration", "ffprobe 能解析但没有时长（%s）" % path, where)
    lines = script.get("lines") if isinstance(script.get("lines"), list) else []
    if len(files) < len(lines):
        _warn(warnings, "tts_partial", "TTS 音频 %d 个 < 剧本 %d 句（可能还没合成完）"
              % (len(files), len(lines)), "tts")
    # ---- 文件名 <-> 行 id 对账（见函数 docstring：只比个数会漏掉"数量相同但对不上"）----
    by_id = {str(ln.get("id")): ln for ln in lines if isinstance(ln, dict)}
    stems = [os.path.splitext(n)[0] for n in files]
    missing = [lid for lid in by_id if lid not in stems]
    extra = [s for s in stems if s not in by_id]
    if missing:
        _warn(warnings, "tts_missing_lines",
              "work/tts 里没有这些句子的音频：%s（剧本里这几句 id 没变、音频却不在；"
              "重跑 --stage tts 会补齐。也可能只是 tts 还没跑完）"
              % "、".join(missing[:8]), "tts", line_ids=missing)
    if extra:
        _warn(warnings, "tts_stale_files",
              "work/tts 里有剧本用不到的音频：%s（旧片缓存/改过 id？它们不会被这次渲染使用）"
              % "、".join(sorted(extra)[:8]), "tts", file_names=sorted(extra)[:8])
    # ---- 时长 <-> 台词长度对账：过短几乎是截断/坏缓存 ----
    short = []
    for name in files:
        dur = durs.get(name)
        ln = by_id.get(os.path.splitext(name)[0])
        if not dur or not isinstance(ln, dict):
            continue
        n_chars = sum(1 for ch in str(ln.get("text") or "") if not ch.isspace())
        if n_chars >= 4 and float(dur) < 0.06 * n_chars:
            short.append("%s %.2fs/%d 字" % (name, float(dur), n_chars))
    if short:
        _warn(warnings, "tts_suspect_short",
              "这些 TTS 音频短得不合理（<0.06 s/字，正常旁白 0.15–0.25 s/字）：%s —— "
              "音频被截断或缓存坏了的典型表现，渲染时这一句会被压没；"
              "用 --stage tts --force-tts 重合成" % "、".join(short[:8]), "tts")
    if bad:
        _check(checks, "tts", "fail", "%d/%d 个音频不可用" % (bad, len(files)))
    else:
        _check(checks, "tts", "pass", "%d 个 mp3 均可解析（时长 %.2f–%.2f s）"
               % (len(files),
                  min(durs.values()) if durs else 0.0,
                  max(durs.values()) if durs else 0.0))


# ---------------------------------------------------------------- 检查：web 页面
def _web_entry(script):
    """engine=web 的入口页：film.json 的 web.entry，或 <剧本目录>/web/index.html。

    与 render.py 的 _web_entry 同口径（web.entry 按当前目录 resolve）。
    """
    web = script.get("web") if isinstance(script.get("web"), dict) else {}
    entry = str(web.get("entry") or "").strip()
    if entry:
        return os.path.abspath(entry)
    base = str(script.get("_dir") or os.getcwd())
    return os.path.abspath(os.path.join(base, "web", "index.html"))


def _skill_web_template():
    """技能自带的 web 模板页（skills/video-studio/assets/web/index.html）；找不到返回 ""。

    preflight.py 在 scripts/ 下，模板资产在兄弟目录 assets/web/。它的用途只有一句：
    engine=web 缺页面时告诉用户"模板在哪"，而不是自己拿模板当项目页面（render 不会用它）。
    """
    p = os.path.normpath(os.path.join(HERE, os.pardir, "assets", "web", "index.html"))
    return p if os.path.isfile(p) else ""


def _check_web_page(script, engine, errors, warnings, checks):
    """engine=web 时入口页必须存在；engine=pil 时本项不适用 = skipped。"""
    if engine not in ("pil", "web"):
        _check(checks, "web_page", "skipped", "engine=%r 非法，未检查" % (engine,))
        return
    if engine == "pil":
        _check(checks, "web_page", "skipped", "engine=pil，不适用")
        return
    entry = _web_entry(script)
    if not os.path.isfile(entry):
        tpl = _skill_web_template()
        # assets/web is a demo page for smoke-testing the web path, not a template.
        # This skill ships no templates: web-path films need the user own page (PIL path is zero-copy).
        if tpl:
            hint = ("（web 引擎需要**你自己的页面**：把技能自带的**示例页**拷到 %s 只能跑通链路，"
                    "它只有 3 段占位场景、不是给你改字就出片的模板；正片请写自己的页面，"
                    "或在 film.json 写 web.entry 指向它。示例页：%s）"
                    % (os.path.dirname(entry), tpl))
        else:
            hint = ("写 film.json 的 web.entry 指向你自己的页面；或改用 PIL 路线"
                    "（--engine pil，不需要任何页面，零拷贝即可出片）")
        _err(errors, "web_page_missing", "engine=web 但入口页不存在：%s（%s）"
             % (entry, hint), "web_page")
        _check(checks, "web_page", "fail", entry)
        return
    try:
        head = open(entry, "rb").read(512).decode("utf-8", "replace")
    except OSError as e:                # noqa: BLE001
        _err(errors, "web_page_unreadable", "入口页读不了：%s（%s）" % (entry, e), "web_page")
        _check(checks, "web_page", "fail", entry)
        return
    if "<html" not in head.lower() and "<!doctype" not in head.lower():
        _warn(warnings, "web_page_not_html",
              "入口页开头不像 HTML（没看到 <html / <!doctype）：%s" % entry, "web_page")
    _check(checks, "web_page", "pass", entry)


# ---------------------------------------------------------------- 总入口
def _timeline_hint(script, timeline, warnings):
    """timeline 与剧本对不上时告警（layout 变了没重排轴，采信旧 timeline 会错帧）。"""
    if not isinstance(timeline, dict):
        return
    scenes = timeline.get("scenes")
    if not isinstance(scenes, list):
        return
    ids = {str(sc.get("id")) for sc in scenes if isinstance(sc, dict)}
    lines = script.get("lines") if isinstance(script.get("lines"), list) else []
    missing = [str(ln.get("id")) for ln in lines
               if isinstance(ln, dict) and str(ln.get("id")) not in ids]
    if missing:
        _warn(warnings, "timeline_stale",
              "timeline 里缺 %d 句（%s），很可能已经过时，请先重跑 --stage layout"
              % (len(missing), " / ".join(missing[:6])), "timeline")


def check(script, *, work=None, engine="pil", timeline=None) -> dict:
    """渲染前资源闸门：返回 {"ok", "errors", "warnings", "checks", "engine"}。

    纯函数：不打印、不落盘、不改入参。ok = 没有 errors（warnings / skipped 不影响 ok）。
    """
    errors, warnings, checks = [], [], {}
    eng = str(engine or "pil").strip().lower()
    n_load_errors = len(errors)
    script_obj = _load_script(script, errors)
    n_script_errors = len(errors) - n_load_errors
    if script_obj is None:
        for cid in CHECK_IDS:
            _check(checks, cid, "skipped", "剧本不可用，未执行")
        return dict(ok=False, errors=errors, warnings=warnings, checks=checks, engine=eng)
    tl = _load_timeline(timeline, warnings)
    if script_obj.get("lines"):
        lines = script_obj["lines"]
        if n_script_errors:
            _check(checks, "script", "fail",
                   "%d 行，但结构有 %d 处问题（见 errors）" % (len(lines), n_script_errors))
        else:
            _check(checks, "script", "pass", "%d 行 / id 唯一" % len(lines))
    else:
        _check(checks, "script", "fail", "lines 不可用")
    if eng not in ("pil", "web"):
        _err(errors, "bad_engine", "engine 只能是 pil 或 web（收到 %r）" % (engine,), "engine")
    _check_glyphs(script_obj, tl, errors, warnings, checks)
    _check_tts(script_obj, work, errors, warnings, checks)
    _check_fonts(errors, warnings, checks)
    _check_web_page(script_obj, eng, errors, warnings, checks)
    _timeline_hint(script_obj, tl, warnings)
    return dict(ok=not errors, errors=errors, warnings=warnings, checks=checks,
                engine=eng, script=str(script_obj.get("_path") or ""),
                work=str(work) if work else "")


# ---------------------------------------------------------------- CLI
def _print_human(res):
    """给人看的输出：检查矩阵 + 每条 error 的定位（字符 / 字段 / 句 id）。"""
    order = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}
    print("== preflight（engine=%s）" % res.get("engine"))
    for cid in CHECK_IDS:
        c = res["checks"].get(cid) or {}
        print("  %-4s %-8s %s" % (order.get(c.get("status"), "?"), cid,
                                  c.get("detail") or ""))
    for w in res.get("warnings") or []:
        print("  [WARN] %s  %s" % (w.get("where") or "", w.get("msg") or ""))
    if res.get("errors"):
        print("  [FAIL] %d 处问题（渲染前必须修）：" % len(res["errors"]))
        for e in res["errors"]:
            print("    - %-18s %s" % (e.get("code"), e.get("msg")))
            if e.get("where"):
                print("      where: %s" % e["where"])
    n_skip = sum(1 for c in res["checks"].values() if c.get("status") == "skipped")
    if n_skip:
        print("  注意：有 %d 项本次**没有检查**（skipped != 通过）" % n_skip)
    print("  结论：%s" % ("FAIL" if res.get("errors") else "PASS"))


def _script_engine(script_path):
    """从 film.json 读 engine 字段（读不到按 "pil"）：CLI 不传 --engine 时与 render.py 一致。"""
    try:
        with open(str(script_path), encoding="utf-8") as fh:
            data = json.load(fh)
        eng = str((data or {}).get("engine") or "pil").strip().lower()
        return eng or "pil"
    except Exception:                   # noqa: BLE001 —— 剧本问题由 check() 统一报
        return "pil"


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="渲染前资源闸门（缺字形 / 坏 TTS / 字体 / web 页面）")
    ap.add_argument("--script", required=True, help="film.json 路径")
    ap.add_argument("--work", default="", help="work 目录（有 work/tts/*.mp3 才校验音频）")
    ap.add_argument("--engine", default="", choices=("", "pil", "web"),
                    help="渲染引擎；缺省读 film.json 的 engine 字段（再缺省 pil）")
    ap.add_argument("--timeline", default="", help="work/timeline.json（可选；补扫场景文本/查过时）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（机读）")
    ap.add_argument("--layout", default="", choices=("", "landscape", "portrait"),
                    help="画布布局：landscape=1920x1080（默认）；portrait=1080x1920"
                         "（experimental：版式骨架与 compare/process/chart 已竖屏原生，"
                         "排版节奏未按竖屏复核）。必须与 film.json 的 video 块宽高比一致")
    ap.add_argument("--strict", action="store_true",
                    help="skipped 也算失败（对齐 render.py --strict-gates 语义）")
    a = ap.parse_args(argv)
    engine = a.engine or _script_engine(a.script)
    res = check(a.script, work=(a.work or None), engine=engine,
                timeline=(a.timeline or None))
    if a.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        _print_human(res)
    n_skip = sum(1 for c in res["checks"].values() if c.get("status") == "skipped")
    if a.strict and n_skip and not res["errors"]:
        if not a.json:
            print("  [FAIL] --strict：%d 项没检查算失败（skipped != 通过）" % n_skip)
        return 1
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
