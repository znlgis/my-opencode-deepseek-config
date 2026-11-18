# -*- coding: utf-8 -*-
"""golden_frames.py — 金帧像素回归。

解决什么问题
------------
改一行绘制代码、换一套设计、动字幕排版，肉眼看不出来、qa_frames 的统计指标也未必报——
只有"同一帧的像素哈希"能当场抓住。本工具就是那条基线。

做什么
------
渲染指定帧号 -> 每帧算 SHA-256 -> 与 skills/video-studio/assets/golden_frames.json 比对。
  * --write   生成/更新基线（绘制代码有意改动后重录，并把 JSON 一起提交）
  * --check   校验（默认）：同环境像素变了 = fail，并列出哪几帧变了

三档语义（"没查"绝不能写成"通过"）
--------------------------------
  pass    字体指纹 + 版本指纹一致，且每一帧 sha256 与基线相同
  fail    同环境（字体/版本一致）但至少一帧像素变了；或渲染本身炸了
  skipped 没法比：基线不存在 / 换了机器（字体指纹不同）/ Pillow·numpy·Python 版本不同 /
          基线帧号超出当前时长。**skipped != pass**，需要人来 --write 重录或核对。

为什么字体不同要 skipped 而不是 fail
------------------------------------
像素哈希对字体二进制是敏感的：msyh.ttc 打了补丁、换台 Win10/Win11 机器字体版本不同，
逐像素就会变，但这不是"渲染器回归"。所以基线里存字体指纹（实际参与渲染的字体文件的
sha1），指纹不同直接 skipped（跨机不误报）；指纹相同还变，才是真回归 -> fail。
版本（Pillow/numpy/Python）同理：跨版本抗锯齿可能变，判 skipped 而不是冤枉渲染代码。

基线怎么来的（确定性）
----------------------
不联网、不依赖 TTS 产物：用固定的假语音时长（FAKE_VOICE_DUR=2.0s）排一条合成时间轴，
取夹具片的 3 个时刻（hook / body / outro）作为默认帧号。这样同一份 film.json + scenes.py
在任何机器上都能立刻重算基线；真实旁白时长只影响"哪一帧是哪一句"，不影响"这一帧画成
什么样"，后者才是本工具要守的东西。帧号与场景 id、段内时刻都记进基线，便于人工复核。

渲染路径与 render.py 对齐：scenes.draw_frame(...) + filmkit.draw_subtitle(...)，
**不含**片头/片尾的淡入淡出（默认帧号刻意避开 <0.5s 与 >total-0.8s 的区间），
所以它回归的是"每个场景画出来的像素"，不是 ffmpeg 编码后的画面。

CLI
---
    python tools/golden_frames.py [--check | --write] [--json] [--strict]
                                  [--script PATH] [--scenes PATH] [--golden PATH]
                                  [--frames 56,168,380]
    退出码：0 = pass 或 skipped；1 = fail；skipped 加 --strict 时为 2。
    --json 输出 {status, frames:[{frame, sha256, expect}], font_fp, ...}，可直接给
    selfcheck.py / CI 消费；给人看的文字一律走 stdout（--json 时只有 JSON）。

依赖：stdlib + numpy + Pillow + 同目录的 filmkit / stylekit / scenes（项目已有依赖，无新增）。
"""
from __future__ import annotations

__version__ = "1.1.0"

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 收编后 tools/ 在技能目录内：ROOT 即技能根（上游是 <repo>/tools + <repo>/skills/video-studio）。
SKILL = ROOT
SCRIPTS = os.path.join(SKILL, "scripts")
ASSETS = os.path.join(SKILL, "assets")

DEFAULT_SCRIPT = os.path.join(ASSETS, "film.json")
# v1.0 / D3：金帧基线绑"固定 fixture 设计"而不是内置风格。
# 为什么：金帧要守的是"同一份设计 + 同一份场景代码，像素不许漂"，
# 不是"某套内置风格长什么样"——后者会随风格表下沉到 presets 而失去意义。
# 没有这个文件时回落到 stylekit 老口径（它现在只会明确报错），不阻塞诊断。
DEFAULT_DESIGN = os.path.join(ASSETS, "design_fixture.json")
DEFAULT_SCENES = os.path.join(ASSETS, "scenes.py")
DEFAULT_GOLDEN = os.path.join(ASSETS, "golden_frames.json")

GOLDEN_VERSION = 1
# 合成时间轴用的假语音时长（秒）：不联网、不读 TTS 产物也能复现
FAKE_VOICE_DUR = 2.0
# 默认三个时刻的段内偏移（秒）：hook 标题已入场 / body 条目已落位 / outro 标题已入场
HOOK_LOCAL = 1.2
BODY_LOCAL = 1.6
OUTRO_LOCAL = 1.5
# 参与字体指纹的字重（内置绘制器只用到 bold / reg / light）
FONT_WEIGHTS = ("bold", "reg", "light")

RESULT_KEYS = ("status", "frames", "font_fp")


# ---------------------------------------------------------------- 基础工具
def _setup_stdio():
    """输出被重定向时切 UTF-8（Windows 控制台默认 GBK，中文会乱码）。与 render.py 同口径。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def _sha1_file(path):
    """文件内容 sha1（读不了返回 "missing"，不抛异常）。"""
    try:
        h = hashlib.sha1()
        with open(str(path), "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return "missing"


def _sha256_image(img):
    """一帧像素的 sha256：先写画布尺寸，再写 RGB 字节（尺寸变了哈希也一定变）。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    h = hashlib.sha256()
    h.update(("RGB:%dx%d:" % (img.size[0], img.size[1])).encode("ascii"))
    h.update(img.tobytes())
    return h.hexdigest()


def _rel(path):
    """尽量把路径写成相对仓库根的 posix 形式（基线文件要跨机器可读）。"""
    try:
        rel = os.path.relpath(os.path.abspath(str(path)), ROOT)
    except ValueError:                                      # 跨盘符
        return os.path.basename(str(path))
    return rel.replace(os.sep, "/")


def _json_bytes(data):
    """确定性 JSON 字节：固定缩进、不排序键（键顺序由构造代码固定）、不带时间戳。"""
    return (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


# ---------------------------------------------------------------- 依赖装载
def _modules():
    """惰性装载 filmkit / stylekit（tools/ 与 scripts/ 不同目录，必须先补 sys.path）。"""
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    try:
        import filmkit as K
        import stylekit as ST
        return K, ST
    except Exception as e:                                  # noqa: BLE001
        raise RuntimeError("装载 filmkit/stylekit 失败（%s）：%s" % (SCRIPTS, e)) from e


def _load_scenes(path):
    """按路径装载 scenes.py（独立模块名，避免与站点包里的同名模块串档）。"""
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise RuntimeError("scenes.py 不存在：%s" % path)
    spec = importlib.util.spec_from_file_location("golden_scenes", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("scenes.py 无法作为模块加载：%s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden_scenes"] = mod
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "draw_frame", None)):
        raise RuntimeError("scenes.py 缺少 draw_frame(scene, t_local, dur, t_global, "
                           "script, frame) 契约：%s" % path)
    return mod


def _load_script(path):
    """读剧本并补上渲染期需要的私有字段（_dir / _path / _total 由调用方补）。"""
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise RuntimeError("剧本不存在：%s" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            script = json.load(fh)
    except ValueError as e:
        raise RuntimeError("剧本不是合法 JSON：%s（%s）" % (path, e)) from e
    if not isinstance(script, dict) or not isinstance(script.get("lines"), list):
        raise RuntimeError("剧本结构不对（需要 dict 且 lines 是数组）：%s" % path)
    script["_path"] = path
    script["_dir"] = os.path.dirname(path)
    return script


# ---------------------------------------------------------------- 时间轴 / 风格
def build_timeline(K, script):
    """由固定假语音时长排一条确定性时间轴（不联网、不读 TTS 产物）。

    与 render.st_layout 的差别只在 dur 的来源：这里每句固定 FAKE_VOICE_DUR 秒，
    其余（lead/tail/min_dur/hold/bar）照常走 filmkit.build_timeline，结果可复现。

    但 **fps 必须跟 render.py 一样取自 filmkit.video_plan**（fps 可按片配置）：
    build_timeline 的默认 fps=30 会让 24/25/50/60 fps 的片子按 30 fps 编号，
    于是基线里的 frame 号既不是成片的真实帧号，t=frame/fps 也不是真实时刻——
    那份基线守的是"另一部片子"的画面，回放/比对都错位（且不会报错）。
    """
    lines = []
    for i, ln in enumerate(script.get("lines") or []):
        if not isinstance(ln, dict):
            continue
        row = dict(ln)
        row.setdefault("id", "l%02d" % (i + 1))
        row["dur"] = float(FAKE_VOICE_DUR)
        lines.append(row)
    if not lines:
        raise RuntimeError("剧本 lines 为空，无法排时间轴")
    tl = K.build_timeline(
        lines,
        lead=float(script.get("lead", 0.55)),
        tail=float(script.get("tail", 0.85)),
        min_dur=float(script.get("min_dur", 3.0)),
        tail_map=script.get("tail_map"),
        lead_map=script.get("lead_map"),
        hold=float(script.get("hold", 0.0)),
        max_dur=script.get("max_dur"),
        fps=K.video_plan(script)["fps"],
    )
    tl["title"] = script.get("title", "")
    return tl


def _design_plan(design_path):
    """读 fixture 设计并转成计划；读不到返回 None（回落到 stylekit 老口径）。"""
    if not design_path or not os.path.exists(str(design_path)):
        return None
    try:
        with open(design_path, encoding="utf-8") as fh:
            design = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError("fixture 设计读不出来（%s）：%s" % (design_path, e)) from e
    sys.path.insert(0, SCRIPTS)
    import designkit as DK
    bad = DK.validate_design(design)
    if bad:
        raise RuntimeError("fixture 设计没过校验（%s）：%s" % (design_path, "；".join(bad)))
    return DK.design_to_plan(design)


def resolve_plan(ST, script, design_path=None):
    """解析计划并挂到 script["_style"]。

    v1.0 / D3 起优先用 **fixture 设计**（design_path，默认 assets/design_fixture.json）：
    金帧守的是"固定设计 + 固定场景代码 -> 像素不漂"，与"哪套内置风格"无关。
    没有 fixture 时回落到 stylekit 老口径，不阻塞老项目。

    主题体检失败在这里就抛错（不要等渲到第 3 帧才炸）。
    """
    plan = _design_plan(design_path)
    if plan is not None:
        script["_style"] = plan
        bad = ST.theme_lint(plan.get("theme") or {})
        if bad:
            raise RuntimeError("fixture 设计主题体检失败：%s" % "；".join(bad))
        return plan
    plan = ST.resolve_script_style(script)
    bad = ST.theme_lint(plan.get("theme") or ST.THEMES[plan["theme_id"]])
    if bad:
        raise RuntimeError("主题体检失败（%s）：%s" % (plan["theme_id"], "；".join(bad)))
    script["_style"] = plan
    return plan


# ---------------------------------------------------------------- 字体指纹
def font_fingerprint(K, plan):
    """按"实际会被用到的字重 + 主题字体族"解析字体，返回 (font_fp, fonts 列表)。

    font_fp = sha1("字体名 sha1 \\n 字体名 sha1 ...")：只依赖字体内容与文件名，
    不依赖绝对路径与 mtime——跨机器只要字体文件相同就得到同一个指纹。
    """
    fam = "sans"
    if not plan.get("legacy"):
        th = plan.get("theme") or {}
        fam = str(th.get("family") or "sans")
    fonts, seen = [], set()
    for wgt in FONT_WEIGHTS:
        try:
            f = K.font(32, wgt, fam)
        except Exception as e:                              # noqa: BLE001
            raise RuntimeError("解析字体失败（weight=%s family=%s）：%s" % (wgt, fam, e)) from e
        path = str(getattr(f, "path", "") or "")
        name = os.path.basename(path) or "?"
        if name in seen:
            continue
        seen.add(name)
        fonts.append({"name": name, "sha1": _sha1_file(path)})
    fonts.sort(key=lambda x: x["name"])
    payload = "\n".join("%s %s" % (x["name"], x["sha1"]) for x in fonts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest(), fonts


def env_fingerprint():
    """运行环境指纹：Pillow / numpy / Python 版本（跨版本像素不保证一致 -> skipped）。

    只把会参与绘制计算的版本算进指纹；平台信息单独记录，仅作诊断（Win10/Win11 同一套
    Pillow + 同字体通常逐像素一致，不必因操作系统不同而 skipped）。

    口径细节：**Python 只按 major.minor 参与指纹**，补丁号只记录不参与比较。
    理由是真正决定抗锯齿像素的是 Pillow（FreeType）与 numpy，不是 CPython 的补丁版本；
    而按补丁号比较会让"作者机器 3.12.16 / 用户机器 3.12.14"这种正常差异一律 skipped——
    实测踩过：金帧回归因此显示"0 帧像素一致"却记 PASS，等于这条门禁从没真的比过。
    真要跨 Pillow/numpy 版本比，仍然会 skipped，那才是像素不可比的情形。
    """
    import numpy
    from PIL import __version__ as pillow_ver
    py_full = "%d.%d.%d" % sys.version_info[:3]
    osname = platform.system()
    env = dict(python=py_full, pillow=pillow_ver, numpy=numpy.__version__, os=osname,
               platform="%s %s" % (osname, platform.release()))
    return _env_fp_from(env), env


def _env_fp_from(env):
    """按"参与比较的口径"给 env 字典算指纹（Python 只取 major.minor）。

    单独抽出来是为了**兼容老基线**：早期 golden_frames.json 里记的是完整补丁号
    （如 3.12.16），如果直接拿新口径算出的指纹去比，老基线会永远 skipped——那等于把
    "补丁号差异"永久固化成"这条门禁不用跑"。这里对**存的 env** 和**当前 env** 走同一个
    归一化函数，所以 3.12.14 与 3.12.16 视为同一环境，而 Pillow/numpy 的差异照旧拦住。
    """
    env = dict(env or {})
    py = str(env.get("python") or "")
    py_mm = ".".join(py.split(".")[:2]) if py else ""
    payload = "python=%s;pillow=%s;numpy=%s;os=%s" % (
        py_mm, env.get("pillow") or "", env.get("numpy") or "", env.get("os") or "")
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 渲染
def _scene_at(tl, t):
    """全片时刻 -> (scene, 段内时刻)，与 render._render_range 的查找方式一致。"""
    scenes = tl["scenes"]
    idx = 0
    while idx + 1 < len(scenes) and t >= scenes[idx + 1]["start"]:
        idx += 1
    s = scenes[idx]
    return s, float(t) - float(s["start"])


def render_frame(K, scenes_mod, script, plan, tl, frame):
    """渲染一帧：draw_frame + draw_subtitle（不含片头片尾 fade，理由见模块文档）。"""
    frame = int(frame)
    if frame < 0 or frame >= int(tl["frames"]):
        raise RuntimeError("帧号 %d 越界（当前时间轴共 %d 帧）" % (frame, tl["frames"]))
    fps = float(tl["fps"])
    t = frame / fps
    scene, lt = _scene_at(tl, t)
    img = scenes_mod.draw_frame(scene, lt, float(scene["dur"]), t, script, frame)
    if img is None:
        raise RuntimeError("场景 %s 在 t=%.3fs 返回 None（draw_frame 必须返回 PIL.Image）"
                           % (scene.get("id"), t))
    if tuple(img.size) != (int(K.W), int(K.H)):
        raise RuntimeError("场景 %s 在 t=%.3fs 返回 %dx%d，期望 %dx%d"
                           % (scene.get("id"), t, img.size[0], img.size[1], K.W, K.H))
    theme = None if plan.get("legacy") else plan.get("theme")
    img = K.draw_subtitle(img, K.sub_at(tl["scenes"], t, script.get("sub_max_hold")),
                          theme=theme)
    return img, scene


def render_frames(script, scenes_mod, plan, tl, frames):
    """渲染一批帧，返回 [(frame, sha256, scene_id)]。

    清空 filmkit 的字体缓存再渲染：保证 font_fingerprint() 看到的是"这些帧真的用了哪些
    字体"，而不是上一次调用留在全局缓存里的残留（同一进程里多次 check 时尤其重要）。
    """
    K, _ST = _modules()
    for cache in (getattr(K, "_font_cache", None),
                  getattr(scenes_mod, "_FONT_CACHE", None)):
        try:
            cache.clear()
        except Exception:                                   # noqa: BLE001
            pass
    script["_total"] = tl["total"]
    out = []
    for frame in frames:
        img, scene = render_frame(K, scenes_mod, script, plan, tl, frame)
        out.append((int(frame), _sha256_image(img), str(scene.get("id"))))
    return out


# ---------------------------------------------------------------- 帧号选择
def default_frame_specs(tl):
    """默认三个时刻（hook / body / outro）；段数很少时退化为同一段取三个时刻。"""
    scenes = tl["scenes"]
    n = len(scenes)
    fps = float(tl["fps"])
    specs = []

    def add(idx, local, label):
        s = scenes[idx]
        dur = float(s["dur"])
        lt = max(0.55, min(float(local), max(0.56, dur - 0.8)))
        frame = int(round((float(s["start"]) + lt) * fps))
        frame = max(1, min(int(tl["frames"]) - 1, frame))
        specs.append(dict(frame=frame, scene=str(s.get("id") or "s%02d" % idx),
                          t_local=round(lt, 3), label=label))

    if n == 1:
        add(0, 0.9, "hook")
        add(0, 1.7, "body")
        add(0, 2.5, "outro")
    else:
        add(0, HOOK_LOCAL, "hook")
        add(max(1, n // 2), BODY_LOCAL, "body")
        add(n - 1, OUTRO_LOCAL, "outro")
    # 去重（超短片 / 单场景时不同时刻可能落到同一帧）：保留第一次出现的标签顺序
    uniq, seen = [], set()
    for sp in specs:
        if sp["frame"] in seen:
            continue
        seen.add(sp["frame"])
        uniq.append(sp)
    return uniq if uniq else [dict(frame=1, scene=str(scenes[0].get("id") or "s00"),
                                   t_local=0.6, label="hook")]


# ---------------------------------------------------------------- 基线读写
def _golden_payload(script_path, scenes_path, K, plan, tl, rendered, design_path=None):
    font_fp, fonts = font_fingerprint(K, plan)
    env_fp, env = env_fingerprint()
    return {
        "version": GOLDEN_VERSION,
        "generator": "golden_frames.py v%s" % __version__,
        "comment": "金帧基线：字体指纹+环境指纹一致时，每一帧像素 sha256 必须相同；"
                   "换机/字体/Pillow 版本不同判 skipped（不误报），同环境像素变了才是 fail。"
                   "改动夹具（film.json / scenes.py / 设计）后请 --write 重录并提交本文件。",
        "canvas": [int(K.W), int(K.H)],
        # 记时间轴自己的 fps（不是写死的 K.FPS）：film.json 可以设 24/25/50/60
        "fps": int(tl["fps"]),
        "script": _rel(script_path),
        "scenes": _rel(scenes_path),
        "design": _rel(design_path) if design_path else "",
        "font_fp": font_fp,
        "fonts": fonts,
        "env_fp": env_fp,
        "env": env,
        "inputs": {
            "script_sha1": _sha1_file(script_path),
            "scenes_sha1": _sha1_file(scenes_path),
            "filmkit_sha1": _sha1_file(os.path.join(SCRIPTS, "filmkit.py")),
            "stylekit_sha1": _sha1_file(os.path.join(SCRIPTS, "stylekit.py")),
            # 设计 fixture 进指纹：改了基准设计却不重录基线 = "改了但基线不报警"
            "design_sha1": _sha1_file(design_path) if design_path else "",
            "designkit_sha1": _sha1_file(os.path.join(SCRIPTS, "designkit.py")),
            "golden_tool_sha1": _sha1_file(os.path.abspath(__file__)),
        },
        "frames": [dict(sp, sha256=sha, scene=scene_id, frame=int(f))
                   for (f, sha, scene_id), sp in zip(rendered, plan["specs"])],
    }


def write_golden(script_path, scenes_path, golden_path, frames=None, design_path=None):
    """--write：渲染金帧并写基线。返回 result（status=pass，written=True）。"""
    K, ST = _modules()
    script = _load_script(script_path)
    scenes_mod = _load_scenes(scenes_path)
    tl = build_timeline(K, script)
    plan = resolve_plan(ST, script, design_path=design_path)
    if frames:
        specs = [dict(frame=int(f), scene=str(_scene_at(tl, int(f) / float(tl["fps"]))[0]
                                       .get("id") or ""),
                      t_local=round(int(f) / float(tl["fps"]) -
                                    float(_scene_at(tl, int(f) / float(tl["fps"]))[0]["start"]), 3),
                      label="custom%d" % i)
                 for i, f in enumerate(frames)]
    else:
        specs = default_frame_specs(tl)
    rendered = render_frames(script, scenes_mod, plan, tl, [sp["frame"] for sp in specs])
    plan = dict(plan, specs=specs)
    data = _golden_payload(script_path, scenes_path, K, plan, tl, rendered,
                           design_path=design_path)
    golden_path = os.path.abspath(str(golden_path))
    d = os.path.dirname(golden_path)
    if d:
        os.makedirs(d, exist_ok=True)
    payload = _json_bytes(data)
    with open(golden_path, "wb") as fh:
        fh.write(payload)
    return {
        "status": "pass",
        "frames": [dict(frame=f, sha256=sha, expect=None, scene=scene_id)
                   for (f, sha, scene_id) in rendered],
        "font_fp": data["font_fp"],
        "font_fp_expect": None,
        "env_fp": data["env_fp"],
        "written": True,
        "golden": golden_path,
        "golden_sha256": hashlib.sha256(payload).hexdigest(),
        "reason": "",
        "notes": [],
        "changed": [],
    }


def _load_golden(golden_path):
    """读基线；不存在/不合法 -> (None, 原因)。"""
    path = os.path.abspath(str(golden_path))
    if not os.path.isfile(path):
        return None, "基线不存在：%s（先跑 --write 生成）" % path
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "基线读不了（%s）：%s" % (path, e)
    if not isinstance(data, dict) or not isinstance(data.get("frames"), list):
        return None, "基线结构不对（缺 frames 数组）：%s" % path
    return data, ""


def _fmt_fp_pair(actual, expect):
    return "%s… vs %s…" % (str(actual)[:12], str(expect)[:12])


def check_golden(script_path, scenes_path, golden_path, design_path=None):
    """--check：渲染基线里的帧号并与基线比对（默认命令）。"""
    golden, why = _load_golden(golden_path)
    if golden is None:
        return dict(status="skipped", frames=[], font_fp="", reason=why)
    if int(golden.get("version") or 0) != GOLDEN_VERSION:
        return dict(status="skipped", frames=[], font_fp="",
                    reason="基线版本 %r != 当前 %d（需 --write 重录）"
                           % (golden.get("version"), GOLDEN_VERSION))
    expects = []
    for row in golden["frames"]:
        if not isinstance(row, dict) or not isinstance(row.get("frame"), int):
            return dict(status="skipped", frames=[], font_fp="",
                        reason="基线 frames 里有一条没有整数 frame：%r" % (row,))
        expects.append(row)
    if not expects:
        return dict(status="skipped", frames=[], font_fp="",
                    reason="基线里没有帧（frames 为空）：先 --write 生成")
    try:
        K, ST = _modules()
        script = _load_script(script_path)
        scenes_mod = _load_scenes(scenes_path)
        tl = build_timeline(K, script)
        plan = resolve_plan(ST, script, design_path=design_path)
        font_fp, fonts = font_fingerprint(K, plan)
        env_fp, env = env_fingerprint()
        # 先做"能不能比"的判定，再渲（省得换机时白渲三帧）
        if str(golden.get("font_fp") or "") != font_fp:
            return dict(
                status="skipped", frames=[], font_fp=font_fp,
                font_fp_expect=golden.get("font_fp"),
                fonts=fonts, fonts_expect=golden.get("fonts"),
                reason="skipped: font differs（字体指纹 %s）——换机/字体版本不同，"
                       "像素比对不成立；确认画面后在当前机器 --write 重录"
                       % _fmt_fp_pair(font_fp, golden.get("font_fp")))
        # 比指纹时对**存的 env** 走同一个归一化（见 _env_fp_from）：这样早期那些
        # 记着完整补丁号的老基线不会因为 3.12.14 vs 3.12.16 永久 skipped。
        golden_env_fp = _env_fp_from(golden.get("env") or {})
        if golden_env_fp != env_fp:
            return dict(
                status="skipped", frames=[], font_fp=font_fp,
                font_fp_expect=golden.get("font_fp"),
                env=env, env_expect=golden.get("env"),
                reason="skipped: env differs（Pillow/numpy/Python 版本不同：%r vs %r）"
                       "——跨版本抗锯齿不保证逐像素一致；在目标环境 --write 重录"
                       % (env, golden.get("env")))
        frames = [int(r["frame"]) for r in expects]
        over = [f for f in frames if f < 0 or f >= int(tl["frames"])]
        if over:
            # 剧本/参数改短了 -> 基线帧号在当前时间轴里不存在。这是"基准失效"，不是画面回归，
            # 按三档语义只能 skipped（拿"没比"当 fail 会天天误报，当 pass 更不行）。
            return dict(status="skipped", frames=[], font_fp=font_fp,
                        font_fp_expect=golden.get("font_fp"),
                        reason="skipped: 基线帧号超出当前时长（%s >= %d 帧）——"
                               "剧本/节奏改过导致时间轴变短，先 --write 重录"
                               % (", ".join(str(f) for f in over[:6]), int(tl["frames"])))
        rendered = render_frames(script, scenes_mod, plan, tl, frames)
    except Exception as e:                                  # noqa: BLE001 —— 渲染炸了是 fail
        return dict(status="fail", frames=[], font_fp="",
                    reason="渲染金帧失败：%s: %s" % (type(e).__name__, e),
                    traceback=traceback.format_exc()[-2000:])
    # 逐帧比对
    rows, changed = [], []
    for (frame, sha, scene_id), exp in zip(rendered, expects):
        expect = str(exp.get("sha256") or "")
        rows.append(dict(frame=frame, sha256=sha, expect=expect or None,
                         scene=scene_id, label=exp.get("label")))
        if not expect or sha != expect:
            changed.append(frame)
    notes = []
    inputs = golden.get("inputs") or {}
    # golden_tool_sha1 只记录不比对：工具自身（打印/JSON 字段）改动不影响像素，
    # 拿它算 stale 会让每次改工具都产出误导性的"输入变过"提示。
    cur_inputs = dict(script_sha1=_sha1_file(script_path),
                      scenes_sha1=_sha1_file(scenes_path),
                      filmkit_sha1=_sha1_file(os.path.join(SCRIPTS, "filmkit.py")),
                      stylekit_sha1=_sha1_file(os.path.join(SCRIPTS, "stylekit.py")),
                      design_sha1=(_sha1_file(design_path) if design_path else ""),
                      designkit_sha1=_sha1_file(os.path.join(SCRIPTS, "designkit.py")))
    stale = [k for k, v in cur_inputs.items()
             if inputs.get(k) and inputs.get(k) != v]
    if changed:
        if stale:
            notes.append("生成基线之后这些输入改过：%s（有意改动请 --write 重录并复核）"
                         % " / ".join(stale))
        return dict(status="fail", frames=rows, font_fp=font_fp,
                    font_fp_expect=golden.get("font_fp"),
                    env=env, reason="像素变了：%d/%d 帧（%s）"
                    % (len(changed), len(rows), ", ".join(str(f) for f in changed[:12])),
                    changed=changed, notes=notes)
    if stale:
        notes.append("输入指纹与基线不同但像素一致（可能是注释/无关字段改动）：%s"
                     % " / ".join(stale))
    return dict(status="pass", frames=rows, font_fp=font_fp,
                font_fp_expect=golden.get("font_fp"), env=env,
                reason="", changed=[], notes=notes)


# ---------------------------------------------------------------- CLI
# ---------------------------------------------------------------- 构图层基线（10 类构图）
#
# 为什么需要一份**单独**的构图层基线：默认金帧用的是夹具 film.json，它只有 hook/body/outro
# 三类，覆盖不到 compare / process / chart 这些"按 scene_type 分派"的绘制器。
# 而竖屏下 compare→上下两栏、process→竖向轴、chart→标签上移，全是这些绘制器里的分支——
# 改了它们，默认金帧**一个像素都不会变**（夹具里根本没有这些类型）。
#
# 所以这里按 scene_type 直接合成 10 个场景，逐类渲一帧取哈希，单独存一份基线文件。
# 它守的是"每类构图长什么样"，与默认金帧（守"整片某个时刻长什么样"）互补。
COMPOSE_SCENES = (
    ("hero", {"id": "cp_hero", "kind": "hook", "scene_type": "hero", "chapter": "01 · 开场",
              "text": "开场一句话。", "big": "开场标题", "sub": "开场字幕"}),
    ("statement", {"id": "cp_statement", "kind": "body", "scene_type": "statement",
                   "text": "一句陈述，用来测试排版宽度。", "big": "陈述标题", "sub": "陈述字幕"}),
    ("list", {"id": "cp_list", "kind": "body", "scene_type": "list", "chapter": "02 · 正文",
              "text": "三条要点。", "big": "要点标题", "sub": "要点字幕",
              "items": [{"text": "条目一"}, {"text": "条目二"}, {"text": "条目三"}]}),
    ("stat", {"id": "cp_stat", "kind": "body", "scene_type": "stat",
              "text": "一个数字。", "big": "数字标题", "stat": {"value": "42", "unit": "%"},
              "stats": [{"value": "42", "unit": "%", "label": "完成度"}]}),
    ("compare", {"id": "cp_compare", "kind": "body", "scene_type": "compare",
                 "text": "两种做法对比。", "big": "对比标题",
                 "sides": {"left": {"head": "甲方案", "items": ["要点一", "要点二"]},
                           "right": {"head": "乙方案", "items": ["要点三", "要点四"]}}}),
    ("process", {"id": "cp_process", "kind": "body", "scene_type": "process",
                 "text": "四个步骤。", "big": "流程标题",
                 "steps": [{"label": "第一步", "note": "说明一"}, {"label": "第二步", "note": "说明二"},
                           {"label": "第三步", "note": "说明三"}, {"label": "第四步", "note": "说明四"}]}),
    ("quote", {"id": "cp_quote", "kind": "quote", "scene_type": "quote",
               "text": "一句引语，用来测试引言排版。", "big": "引语正文"}),
    ("chapter", {"id": "cp_chapter", "kind": "body", "scene_type": "chapter",
                 "chapter": "03 · 章节", "big": "章节标题", "kicker": "SECTION"}),
    ("chart", {"id": "cp_chart", "kind": "body", "scene_type": "chart",
               "text": "一组数据。", "big": "数据标题",
               "series": [{"label": "甲", "value": 3.0, "unit": "s"},
                          {"label": "乙", "value": 7.5, "unit": "s"},
                          {"label": "丙", "value": 1.2, "unit": "s"}]}),
    ("outro", {"id": "cp_outro", "kind": "outro", "scene_type": "outro",
               "text": "结束语。", "big": "结束标题", "sub": "结束字幕"}),
)


def _compose_default_golden(golden_path=None):
    """构图层基线的文件名：**固定按 profile 取**，不从 --golden 派生。

    踩过一次：`--layout portrait` 已经把 --golden 改成了 golden_frames_portrait.json，
    这里再按 profile 追一次后缀，就变成 golden_frames_portrait_portrait.json——
    两个 profile 各自写了一份名字互不相同的基线，谁都对不上谁。
    所以构图层基线用**自己的固定名字**（golden_frames_compose.json / _compose_portrait.json），
    与整片时刻基线（golden_frames*.json）完全分开。
    """
    suffix = "_portrait" if _is_portrait() else ""
    return os.path.join(ASSETS, "golden_frames_compose%s.json" % suffix)


def _is_portrait():
    """当前画布是不是竖屏。**只看 filmkit 的 W/H**，不看环境变量：

    环境变量可能没设（默认横屏），而 W/H 是 layoutkit 解析后的**事实**。
    """
    K, _ST = _modules()
    return int(K.H) > int(K.W)


def compose_frames(scenes_path, golden_path, design_path=None, mode="check"):
    """按 scene_type 逐类渲一帧并比对/写入构图层基线。

    mode: "check" 比对 / "write" 重录。返回与 check_golden 同形状的 result。
    """
    K, ST = _modules()
    scenes_mod = _load_scenes(scenes_path)
    plan = resolve_plan(ST, {}, design_path=design_path)
    script = {"title": "构图基线", "lines": []}
    if plan:
        script["_style"] = plan
    font_fp, fonts = font_fingerprint(K, plan)
    env_fp, env = env_fingerprint()
    rows = []
    for name, scene in COMPOSE_SCENES:
        img = scenes_mod.draw_frame(dict(scene), 1.5, 6.0, 1.5, script, 45)
        if img is None:
            raise RuntimeError("构图 %s 返回 None" % name)
        if tuple(img.size) != (int(K.W), int(K.H)):
            raise RuntimeError("构图 %s 渲染出 %dx%d，期望 %dx%d"
                               % (name, img.size[0], img.size[1], K.W, K.H))
        rows.append({"name": name, "sha256": _sha256_image(img),
                     "size": [int(K.W), int(K.H)]})

    golden_path = _compose_default_golden()
    data = {
        "version": GOLDEN_VERSION,
        "generator": "golden_frames.py compose v%s" % __version__,
        "comment": "构图层基线：按 scene_type 逐类渲一帧。与 golden_frames*.json（整片时刻基线）互补——夹具片只有 hook/body/outro，覆盖不到 compare/process/chart 这些分派绘制器。",
        "profile": "portrait" if _is_portrait() else "landscape",
        "canvas": [int(K.W), int(K.H)],
        "scenes": _rel(scenes_path),
        "design": _rel(design_path) if design_path else "",
        "font_fp": font_fp,
        "fonts": fonts,
        "env_fp": env_fp,
        "env": env,
        "inputs": {"scenes_sha1": _sha1_file(scenes_path),
                   "filmkit_sha1": _sha1_file(os.path.join(SCRIPTS, "filmkit.py")),
                   "stylekit_sha1": _sha1_file(os.path.join(SCRIPTS, "stylekit.py")),
                   "design_sha1": _sha1_file(design_path) if design_path else ""},
        "frames": rows,
    }

    if mode == "write":
        d = os.path.dirname(golden_path)
        if d:
            os.makedirs(d, exist_ok=True)
        payload = _json_bytes(data)
        with open(golden_path, "wb") as fh:
            fh.write(payload)
        return {"status": "pass", "written": True, "golden": golden_path,
                "frames": [dict(frame=r["name"], sha256=r["sha256"], expect=None,
                                 scene=r["name"]) for r in rows],
                "font_fp": font_fp, "font_fp_expect": None, "reason": "", "notes": []}

    golden, why = _load_golden(golden_path)
    if golden is None:
        return dict(status="skipped", frames=[], font_fp=font_fp, reason=why)
    if str(golden.get("font_fp") or "") != font_fp:
        return dict(status="skipped", frames=[], font_fp=font_fp,
                    font_fp_expect=golden.get("font_fp"),
                    reason="skipped: font differs——换机后在当前机器 --compose --write 重录")
    if _env_fp_from(golden.get("env") or {}) != env_fp:
        return dict(status="skipped", frames=[], font_fp=font_fp,
                    font_fp_expect=golden.get("font_fp"), env=env,
                    env_expect=golden.get("env"),
                    reason="skipped: env differs（Pillow/numpy/Python 版本不同）")
    expect = {str(r.get("name")): str(r.get("sha256")) for r in (golden.get("frames") or [])
              if isinstance(r, dict)}
    out, changed = [], []
    for r in rows:
        want = expect.get(r["name"])
        out.append(dict(frame=r["name"], sha256=r["sha256"], expect=want, scene=r["name"]))
        if want is None or want != r["sha256"]:
            changed.append(r["name"])
    notes = []
    if changed:
        notes.append("构图基线变了：%s（改绘制器是有意的就 --compose --write 重录并复核）"
                     % "、".join(changed))
    return {"status": "fail" if changed else "pass", "frames": out, "font_fp": font_fp,
            "font_fp_expect": golden.get("font_fp"), "changed": changed,
            "notes": notes, "reason": "", "golden": golden_path}


def _print_human(res):
    st = str(res.get("status") or "").upper()
    if res.get("written"):
        print("== golden_frames（写入基线）")
    else:
        print("== golden_frames（%s）" % {"PASS": "像素一致", "FAIL": "像素变化",
                                         "SKIPPED": "跳过（没比）"}.get(st, st))
    for row in res.get("frames") or []:
        exp = row.get("expect")
        got = row.get("sha256") or ""
        if res.get("written"):
            mark = "REC "
        else:
            mark = "OK  " if exp and got == exp else ("DIFF" if exp else "????")
        print("  %s frame=%-6s %s %s  %s"
              % (mark, row.get("frame"), (exp or "(无基线)")[:12], got[:12],
                 row.get("label") or row.get("scene") or ""))
    if res.get("reason"):
        print("  %s" % res["reason"])
    for n in res.get("notes") or []:
        print("  [NOTE] %s" % n)
    if res.get("written"):
        print("  已写入基线：%s（%d 帧，font_fp=%s…）"
              % (res.get("golden"), len(res.get("frames") or []),
                 str(res.get("font_fp"))[:12]))
    if res.get("status") == "skipped":
        print("  注意：skipped != 通过——本次**没有**完成像素比对")


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="金帧像素回归（渲染固定帧号 -> sha256 -> 比对基线）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="校验（默认）")
    g.add_argument("--write", action="store_true", help="生成/更新基线")
    ap.add_argument("--script", default=DEFAULT_SCRIPT, help="film.json（默认夹具片）")
    ap.add_argument("--design", default=DEFAULT_DESIGN,
                    help="固定 fixture 设计（默认 assets/design_fixture.json）；"
                         "传空串回落到老的 stylekit 口径")
    ap.add_argument("--scenes", default=DEFAULT_SCENES, help="scenes.py（默认内置场景）")
    ap.add_argument("--golden", default=DEFAULT_GOLDEN,
                    help="基线 JSON（默认 skills/video-studio/assets/golden_frames.json）")
    ap.add_argument("--frames", default="",
                    help="--write 时指定帧号（逗号分隔）；缺省取夹具片 3 个时刻")
    ap.add_argument("--layout", default="", choices=("", "landscape", "portrait"),
                    help="画布 profile：landscape（默认，基线 golden_frames.json）/"
                         "portrait（experimental，基线 golden_frames_portrait.json）")
    ap.add_argument("--compose", action="store_true",
                    help="构图层基线：按 scene_type 逐类渲一帧（覆盖夹具片没有的 compare/process/chart）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON（机读）")
    ap.add_argument("--strict", action="store_true",
                    help="skipped 用退出码 2 表示（默认 skipped 退出码 0，但绝不写 pass）")
    a = ap.parse_args(argv)
    # 画布 profile 必须在**任何 import filmkit/scenes 之前**落到环境变量：
    # scenes.py 写的是 `from filmkit import W, H`，值在导入那一刻就绑死了。
    # 等到 _modules() 里面再设，渲染出来的仍是横屏——而基线文件名却是 portrait，
    # 于是"竖屏基线"里存的是横屏像素，回归从此永远绿（最坏的那种假通过）。
    if a.layout:
        os.environ["VIDEO_STUDIO_LAYOUT"] = a.layout
        if a.golden == DEFAULT_GOLDEN:
            a.golden = os.path.join(
                ASSETS, "golden_frames.json" if a.layout == "landscape"
                else "golden_frames_%s.json" % a.layout.replace("-", "_"))
    try:
        if a.compose:
            res = compose_frames(a.scenes, a.golden, design_path=(a.design or None),
                                 mode=("write" if a.write else "check"))
        elif a.write:
            frames = None
            if a.frames:
                try:
                    frames = [int(x) for x in a.frames.replace(" ", "").split(",") if x]
                except ValueError:
                    raise SystemExit("--frames 必须是逗号分隔的整数帧号，收到 %r" % a.frames) from None
                if not frames:
                    raise SystemExit("--frames 里没有有效帧号")
            res = write_golden(a.script, a.scenes, a.golden, frames,
                       design_path=(a.design or None))
        else:
            if a.frames:
                raise SystemExit("--frames 只用于 --write；--check 的帧号由基线决定"
                                 "（要换帧号请 --write --frames ... 重录）")
            res = check_golden(a.script, a.scenes, a.golden,
                       design_path=(a.design or None))
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        res = dict(status="fail", frames=[], font_fp="",
                   reason="%s: %s" % (type(e).__name__, e))
    if a.json:
        out = {k: res.get(k) for k in RESULT_KEYS}
        for k in ("status", "frames", "font_fp"):
            if k not in out:
                out[k] = None if k != "frames" else []
        for k in ("font_fp_expect", "env", "env_expect", "reason", "notes",
                  "changed", "written", "golden", "golden_sha256", "fonts",
                  "fonts_expect", "traceback"):
            if k in res:
                out[k] = res[k]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        _print_human(res)
    status = str(res.get("status") or "fail")
    if status == "pass":
        return 0
    if status == "skipped":
        return 2 if a.strict else 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
