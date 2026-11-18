# -*- coding: utf-8 -*-
"""filmkit.py — DeepSeek 4.1 Flash + OpenCode 视频生产内核。

设计原则（全部来自真实成片的复盘）：
1. 质检门禁必须真的会失败。门禁失效比没有门禁更危险。
2. 缓存必须校验输入。只判断"文件存在"会让"改一句台词不生效"静默发生。
3. 失败要报错，不要静默降级。尺寸不符、缺字形、峰值爆表都要看得见。
4. 公共函数可独立测试：不依赖当前目录、不依赖上一次调用留下的状态。

声音与提示点：
5. CueBus：画面登记提示点 -> work/cues.json -> 按提示点合成音效轨（声音跟着画面走，不靠"对齐猜"）。
6. 立体声总线：AudioBus(channels=2) / wav_write 自动识别单双声道；配乐与音效可以声像。
7. hold：每句语音结束后额外停留秒数（"让观众看清楚"的停留窗口，不用整片放慢）。
8. mix_film / duck：旁白 + 配乐 + 音效三总线混音，音效响起时配乐自动让位。
"""
from __future__ import annotations

__version__ = "1.1.0"

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 本文件可能被两种方式加载：`python filmkit.py`（自身目录在 sys.path），
# 或 `from filmkit import ...`（调用方已把 scripts/ 插进 sys.path）。
# 为了在两种情况下都能 import 同目录的 layoutkit，这里显式兜一次。
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

# 画布与安全区来自 layoutkit（单一真源）：这里**不再**写死数字。
# 以前 W/H/SAFE/SUB_BAND 在这个文件顶部各写一遍，而 scenes.py / preflight / qa_frames
# 又各自 from filmkit import 复制一份值——"改画布尺寸"于是变成"找到所有副本"。
# layoutkit 的默认 profile 与原来逐字节相同（golden_frames 守这条）。
#
# SUB_MAX_CHARS 在本文件里没有读取点（filmkit 自己算折行时用的是 layoutkit 的
# 其它常量），但它是**刻意 re-export** 的：selfcheck 第 73 条要把 filmkit 导出的
# 这几个画布常量逐个与 layoutkit 比对，防止有人把数字写回 filmkit 造成两处漂移。
# 所以 lint 的 F401 在这里是误报——删掉它会让第 73 条直接 AttributeError。
from layoutkit import H, SAFE, SUB_BAND, SUB_MAX_CHARS, TYPE_SCALE, W  # noqa: E402,F401

FPS = 30
SR = 48000
SUB_MAX_HOLD = None                 # None = 字幕覆盖整句（默认）；设数字则钳住最大停留秒数

# 提示点里好写的语义别名 -> sfxkit 配方名（sfxkit 自己也有同一张表，两边保持一致）
CUE_ALIASES = {
    "blip": "ui_blip", "click": "ui_click", "relay": "relay_click", "chirp": "data_chirp",
    "sub": "sub_drop", "reverse": "reverse_swell", "rewind": "tape_rewind",
    "stone": "go_stone", "typing": "typing", "stream": "text_stream", "wind": "wind",
}


# ---------------------------------------------------------------- ffmpeg
# ffmpeg 探测必须 fail-closed：候选"文件存在"不等于"能用"。
# 一个只"存在"却没 xfade 的旧构建（例如 C:\Program Files\EVCapture\ffmpeg.exe）
# 会一路跑到滤镜阶段才挂，报错离原因太远。所以每个候选都必须真的跑通
# "ffmpeg -version" 且 returncode == 0 才算可用（超时/异常一律视为不可用）。
_FFMPEG_ENV = "FILMKIT_FFMPEG"
_FFMPEG_STRICT_ENV = "FILMKIT_FFMPEG_STRICT"
_FFMPEG_UP_LEVELS = 5           # 从 cwd 向上最多找 5 层父目录的便携布局
_FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg", "ffmpeg.exe")

# 来源键 -> 给人看的说明（ffmpeg_source() 用；backend 回显 / 日志都靠它）
FFMPEG_SOURCE_LABELS = {
    "env": "显式指定（FILMKIT_FFMPEG）",
    "portable": "向上搜索（tools/ffmpeg）",
    "hardcoded": "硬编码候选（WinGet/系统路径）",
    "path": "PATH（shutil.which）",
    "fallback": "未找到（回退裸命令，默认宽松模式）",
}
_FFMPEG_CACHE = {}              # {候选集合 tuple: (可执行路径, 来源键)}：同一候选集合只探测一次
_FFPROBE_CACHE = {}             # {ffmpeg 路径: ffprobe 路径}：ffprobe 定位也不重复做

_FFMPEG_STRICT_HINT = (
    "找不到可用的 ffmpeg：FILMKIT_FFMPEG、从当前目录向上 %d 层的 tools/ffmpeg、"
    "WinGet/系统固定路径、PATH 里都没有能跑通 \"ffmpeg -version\"（returncode=0）的可执行文件。"
    "装了没有？winget install Gyan.FFmpeg 可以装；装在哪？便携版放到 <项目>/tools/ffmpeg/ffmpeg.exe "
    "即可被自动发现；怎么指？设置环境变量 FILMKIT_FFMPEG=D:/tools/ffmpeg.exe。"
    "（当前 FILMKIT_FFMPEG_STRICT=1，所以直接退出；去掉该变量会回退成裸命令 ffmpeg。）"
    % _FFMPEG_UP_LEVELS
)


def _probe_ffmpeg(path):
    """候选是否真的可用：返回可执行路径，不可用返回 None（fail-closed）。

    必须跑通 "-version"；文件不存在 / 不是可执行文件 / 权限拒绝 / 超时 / 非 0 退出，
    一律视为不可用。异常吞掉继续试下一个候选——探测阶段不应该把 import 炸掉。
    """
    try:
        if not path:
            return None
        exe = str(path)
        if not os.path.isfile(exe):
            exe = shutil.which(exe) or ""   # 允许 FILMKIT_FFMPEG=ffmpeg 这种裸名字
            if not exe:
                return None
        elif not os.path.isabs(exe):
            exe = os.path.abspath(exe)      # 相对路径候选按 cwd 解析，别让 execvp 去 PATH 里找
        p = subprocess.run([exe, "-version"], capture_output=True, timeout=10)
        if p.returncode != 0:
            return None
        return exe
    except Exception:                       # noqa: BLE001 —— 探测失败=候选不可用，换下一个
        return None


def _iter_up_dirs(levels=None):
    """cwd + 向上 levels 层父目录（含 cwd 共 levels+1 个）。cwd 被删时安全退化。"""
    try:
        d = os.path.abspath(os.getcwd())
    except Exception:                       # noqa: BLE001
        return
    yield d
    for _ in range(int(_FFMPEG_UP_LEVELS if levels is None else levels)):
        parent = os.path.dirname(d)
        if parent == d:                     # 到盘符根 / 文件系统根，停
            break
        d = parent
        yield d


def _portable_ffmpeg_candidates():
    """便携布局：<目录>/tools/ffmpeg/ffmpeg(.exe) 与 <目录>/tools/ffmpeg(.exe)。"""
    out = []
    for d in _iter_up_dirs():
        for name in _FFMPEG_NAMES:
            out.append(os.path.join(d, "tools", "ffmpeg", name))
        for name in _FFMPEG_NAMES:
            out.append(os.path.join(d, "tools", name))
    return out


def _hardcoded_ffmpeg_candidates():
    """原有硬编码候选（WinGet Links / Unix 常见路径）。顺序保持原样，不新增 EVCapture 之类的意外构建。"""
    out = []
    local = os.environ.get("LOCALAPPDATA", "")
    user = os.environ.get("USERPROFILE", "")
    if local:
        out.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    if user:
        out.append(os.path.join(user, "AppData", "Local", "Microsoft", "WinGet",
                                "Links", "ffmpeg.exe"))
    out += ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
    return out


def _ffmpeg_candidates():
    """按优先级返回 [(候选路径, 来源键)]：显式指定 -> 向上搜索 -> 硬编码 -> PATH。"""
    cands, seen = [], set()

    def add(path, src):
        path = (path or "").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1].strip()       # 容忍用户把路径连引号一起粘进环境变量
        if not path:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        cands.append((path, src))

    add(os.environ.get(_FFMPEG_ENV), "env")
    for p in _portable_ffmpeg_candidates():
        add(p, "portable")
    for p in _hardcoded_ffmpeg_candidates():
        add(p, "hardcoded")
    add(shutil.which("ffmpeg"), "path")
    return cands


def _resolve_ffmpeg():
    """探测并缓存 ffmpeg，返回 (可执行路径, 来源键)。

    默认宽松：一个候选都不可用时回退裸命令 "ffmpeg"（让真正调用 subprocess 的地方自己报错）；
    FILMKIT_FFMPEG_STRICT=1 时改为 raise SystemExit，并在消息里说清怎么装/放哪/怎么指。
    """
    cands = _ffmpeg_candidates()
    key = tuple((os.path.normcase(os.path.abspath(p)), s) for p, s in cands)
    hit = _FFMPEG_CACHE.get(key)
    if hit is not None:
        # 回退结果命中缓存时也要按"当前"STRICT 再判一次：改完 env 不 reset 也不能漏掉 fail-closed
        if hit[1] != "fallback" or (os.environ.get(_FFMPEG_STRICT_ENV) or "").strip() != "1":
            return hit
        raise SystemExit(_FFMPEG_STRICT_HINT)
    for cand, src in cands:
        exe = _probe_ffmpeg(cand)
        if exe:
            res = (exe if isinstance(exe, str) else cand, src)
            _FFMPEG_CACHE[key] = res
            return res
    if (os.environ.get(_FFMPEG_STRICT_ENV) or "").strip() == "1":
        raise SystemExit(_FFMPEG_STRICT_HINT)
    res = ("ffmpeg", "fallback")
    _FFMPEG_CACHE[key] = res
    return res


def _find_ffmpeg() -> str:
    """内部接口：返回探测到的 ffmpeg 可执行路径。"""
    return _resolve_ffmpeg()[0]


def _reset_ffmpeg_cache():
    """清空探测缓存并立即重探（测试改过 os.environ / cwd 后必须调它）。

    同时刷新模块级 FFMPEG，保证 filmkit.FFMPEG、ffprobe_path()、向上搜索布局
    在同一个进程里改环境后能拿到新结果。
    """
    global FFMPEG
    _FFMPEG_CACHE.clear()
    _FFPROBE_CACHE.clear()
    FFMPEG = _find_ffmpeg()
    return FFMPEG


FFMPEG = _find_ffmpeg()
PY = sys.executable


def _find_ffprobe(ffmpeg=None) -> str:
    """ffprobe 定位：先找 ffmpeg 同目录的 ffprobe(.exe)，再用 shutil.which 兜底。

    不做 FFMPEG.replace("ffmpeg", "ffprobe")：ffmpeg 被改名（如 ffmpeg-full.exe）时
    字符串替换出来的路径根本不存在，调用要到远处才炸。
    """
    ff = str(ffmpeg or FFMPEG)
    hit = _FFPROBE_CACHE.get(ff)
    if hit:
        return hit
    cand = ""
    looks_pathy = os.path.isabs(ff) or os.sep in ff or bool(os.altsep and os.altsep in ff)
    if looks_pathy:
        d = os.path.dirname(os.path.abspath(ff))
        names = ("ffprobe.exe", "ffprobe") if os.name == "nt" else ("ffprobe", "ffprobe.exe")
        for name in names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                cand = p
                break
    if not cand:
        cand = shutil.which("ffprobe") or "ffprobe"
    _FFPROBE_CACHE[ff] = cand
    return cand


def ffprobe_path() -> str:
    """ffprobe 可执行文件路径（按 ffmpeg 路径缓存；_reset_ffmpeg_cache() 可清）。"""
    return _find_ffprobe()


def ffmpeg_source() -> str:
    """可读的 ffmpeg 来源回显，如 "显式指定（FILMKIT_FFMPEG）: D:/tools/ffmpeg.exe"。

    供 backend / 日志说明"到底用了哪个 ffmpeg、它从哪来的"。
    """
    path, src = _resolve_ffmpeg()
    txt = "%s: %s" % (FFMPEG_SOURCE_LABELS.get(src, src), path)
    env = (os.environ.get(_FFMPEG_ENV) or "").strip()
    if env and src != "env":
        txt += "；注意 %s=%r 不可用，已跳过" % (_FFMPEG_ENV, env)
    return txt


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


# ---------------------------------------------------------------- 字体与字形
_font_cache = {}
# ---------------------------------------------------------------------------
# 字体候选：**只在这里写一次**。
#
# 以前 FONT_CANDIDATES 与 FONT_FAMILIES["sans"] 各抄一份同一批路径，
# 于是"加一个字体"要改两处、漏改一处就会出现「用 A 字体量宽度、用 B 字体绘制」
# （文字溢出安全区）。现在 sans 族由本表派生，单一真源。
#
# 顺序即优先级：先 Windows 自带（msyh 一族覆盖最全），再 Linux Noto，
# 最后 macOS PingFang（放在末尾是因为它没有独立的 bold 字重文件）。
# mono 只保证 ASCII；中文请勿用 mono（consola 无中文字形，会画成方块）。
FONT_CANDIDATES = {
    "bold": [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             "/System/Library/Fonts/PingFang.ttc"],
    "reg": [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc"],
    "light": [r"C:\Windows\Fonts\msyhl.ttc", r"C:\Windows\Fonts\msyh.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "mono": [r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\msyh.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
}


# ---------------------------------------------------------------- 字体族
# 换片要换气质，字体是第一眼。但"换字体族"必须同时换**所有**缓存键，
# 否则会出现"用 A 字体量宽度、用 B 字体绘制"——文字直接溢出安全区。
# 每一族都给中文回退：找不到 serif/kai 时退到同族 reg 再退 sans，**绝不退到 mono**（无中文字形）。
FONT_FAMILIES = {
    # sans 由 FONT_CANDIDATES 派生（单一真源）：mono 不进族表，它没有中文字形。
    "sans": {k: list(v) for k, v in FONT_CANDIDATES.items() if k != "mono"},
    "serif": {                     # 杂志 / 史诗：宋体系
        "bold": [r"C:\Windows\Fonts\NotoSerifSC-VF.ttf", r"C:\Windows\Fonts\simsun.ttc",
                 r"C:\Windows\Fonts\msyhbd.ttc"],
        "reg": [r"C:\Windows\Fonts\NotoSerifSC-VF.ttf", r"C:\Windows\Fonts\simsun.ttc",
                r"C:\Windows\Fonts\msyh.ttc"],
        "light": [r"C:\Windows\Fonts\NotoSerifSC-VF.ttf", r"C:\Windows\Fonts\simsun.ttc",
                  r"C:\Windows\Fonts\msyh.ttc"],
    },
    "kai": {                       # 手绘纸感：楷体
        "bold": [r"C:\Windows\Fonts\simkai.ttf", r"C:\Windows\Fonts\simfang.ttf",
                 r"C:\Windows\Fonts\msyhbd.ttc"],
        "reg": [r"C:\Windows\Fonts\simkai.ttf", r"C:\Windows\Fonts\simfang.ttf",
                r"C:\Windows\Fonts\msyh.ttc"],
        "light": [r"C:\Windows\Fonts\simkai.ttf", r"C:\Windows\Fonts\msyh.ttc"],
    },
    "round": {                     # 圆润：等线 / 雅黑回退
        "bold": [r"C:\Windows\Fonts\Dengb.ttf", r"C:\Windows\Fonts\msyhbd.ttc"],
        "reg": [r"C:\Windows\Fonts\Deng.ttf", r"C:\Windows\Fonts\msyh.ttc"],
        "light": [r"C:\Windows\Fonts\Dengl.ttf", r"C:\Windows\Fonts\msyhl.ttc"],
    },
}
FAMILY_IDS = tuple(FONT_FAMILIES.keys())


def font(size: int, weight: str = "bold", family: str = "sans") -> ImageFont.FreeTypeFont:
    """取字体。family 缺省 sans，所以老调用 font(46, "reg") 输出不变。"""
    fam = str(family or "sans")
    if fam not in FONT_FAMILIES:
        fam = "sans"                 # 未知族名退到 sans，不让拼写错误炸掉渲染
    key = (int(size), weight, fam)
    if key in _font_cache:
        return _font_cache[key]
    chain = list(FONT_FAMILIES[fam].get(weight) or [])
    chain += list(FONT_FAMILIES[fam]["reg"])
    chain += list(FONT_CANDIDATES.get(weight) or [])
    chain += list(FONT_CANDIDATES["reg"])
    for path in chain:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, int(size), index=0)
                _font_cache[key] = f
                return f
            except Exception:
                continue
    raise RuntimeError("找不到中文字体，请在 filmkit.FONT_CANDIDATES 里补路径")


_PRIVATE = "\ue000"          # 私用区码点：任何字体都缺字形，作 .notdef 基准
_glyph_sig = {}


def _mask_sig(f, ch):
    try:
        m = f.getmask(ch)
        return (tuple(m.size), bytes(m))
    except Exception:
        return None


def glyph_missing_chars(text: str, size: int = 32, weight: str = "reg"):
    """返回字体缺字形的字符（缺字会被画成 .notdef 方块，脚本零报错）。

    实测：msyhbd 缺 ✓ × >= <= 等；所以 qa 会自动扫剧本里的上屏文字。
    """
    if not text:
        return []
    f = font(size, weight)
    key = (int(size), weight)
    if key not in _glyph_sig:
        _glyph_sig[key] = _mask_sig(f, _PRIVATE)
    ref = _glyph_sig[key]
    miss, seen = [], set()
    for ch in text:
        if ch in seen or ch.isspace():
            continue
        seen.add(ch)
        sig = _mask_sig(f, ch)
        if sig is None or sig == ref:
            miss.append(ch)
    return miss


def check_glyphs(lines, fields=("text", "sub", "big", "lead", "kicker", "note")):
    """扫剧本所有上屏文字，返回 [(id, 字段, 缺字符)]。"""
    errs = []
    for ln in lines:
        for fld in fields:
            v = ln.get(fld)
            if isinstance(v, str) and v:
                miss = glyph_missing_chars(v, 32, "bold")
                if miss:
                    errs.append((ln.get("id", "?"), fld, "".join(miss)))
    return errs


# ---------------------------------------------------------------- 缓动 / 确定性随机
def clamp(x, a=0.0, b=1.0):
    return a if x < a else (b if x > b else x)


def lerp(a, b, p):
    return a + (b - a) * p


def smooth(p):
    p = clamp(p)
    return p * p * (3 - 2 * p)


def ease_out(p):
    p = clamp(p)
    return 1 - (1 - p) ** 3


def ease_in(p):
    p = clamp(p)
    return p ** 3


def ease_in_out(p):
    p = clamp(p)
    return 3 * p * p - 2 * p * p * p


def ease_out_back(p, s=1.7):
    p = clamp(p) - 1
    return 1 + p * p * ((s + 1) * p + s)


def rng(t: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return clamp((t - lo) / (hi - lo))


def _h(n) -> float:
    x = (int(n) * 374761393 + 668265263) & 0xFFFFFFFF
    x = (x ^ (x >> 13)) * 1274126177 & 0xFFFFFFFF
    return ((x ^ (x >> 16)) & 0xFFFFFF) / float(0xFFFFFF)


def hash2(n, m) -> float:
    return _h(int(n) * 73856093 ^ int(m) * 19349663)


def hash3(n, m, k) -> float:
    return _h(int(n) * 73856093 ^ int(m) * 19349663 ^ int(k) * 83492791)


def vnoise(x: float, seed: int = 0) -> float:
    i = math.floor(x)
    f = x - i
    a, b = hash2(i, seed), hash2(i + 1, seed)
    return a + (b - a) * smooth(f)


# ---------------------------------------------------------------- 文本排版
_PUNCT_TAIL = "，。、；：？！）》”’"


def text_w(s: str, f) -> float:
    if not s:
        return 0.0
    bb = f.getbbox(s)
    return bb[2] - bb[0]


def wrap_cn(s: str, f, max_w: float):
    lines, cur = [], ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if text_w(trial, f) <= max_w or not cur:
            cur = trial
        else:
            if ch in _PUNCT_TAIL:
                lines.append(cur + ch)
                cur = ""
            else:
                lines.append(cur)
                cur = ch
    if cur:
        lines.append(cur)
    return lines


_wrap_cache = {}
_tw_cache = {}


def wrap_cn_cached(s: str, size: int, max_w: float, weight: str = "reg",
                   family: str = "sans"):
    """带缓存的换行。场景函数每帧都要调用，务必用这个版本。

    实测：每帧重新排版（12 张卡片 x 2 个 wrap_cn）会让单帧从 4.6ms 涨到 12ms。
    """
    key = (s, int(size), int(max_w), weight, str(family or "sans"))
    r = _wrap_cache.get(key)
    if r is None:
        r = wrap_cn(s, font(size, weight, family), max_w)
        _wrap_cache[key] = r
    return r


def text_w_cached(s: str, size: int, weight: str = "reg", family: str = "sans") -> float:
    key = (s, int(size), weight, str(family or "sans"))
    r = _tw_cache.get(key)
    if r is None:
        r = text_w(s, font(size, weight, family))
        _tw_cache[key] = r
    return r


def clear_text_cache():
    _wrap_cache.clear()
    _tw_cache.clear()


def draw_multiline(img, xy, lines, f, fill, align="left", line_gap=1.35):
    d = ImageDraw.Draw(img, "RGBA")
    lh = int(f.size * line_gap)
    x, y = xy
    boxes = []
    for i, ln in enumerate(lines):
        lw = text_w(ln, f)
        px = x - lw / 2 if align == "center" else (x - lw if align == "right" else x)
        d.text((px, y + i * lh), ln, font=f, fill=fill)
        boxes.append((px, y + i * lh, px + lw, y + i * lh + f.size))
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def check_bbox(box, safe=SAFE, tag=""):
    x0, y0, x1, y1 = box
    sx0, sy0, sx1, sy1 = safe
    if x0 < sx0 or x1 > sx1 or y0 < sy0 or y1 > sy1:
        return ["[%s] 越界: (%s) vs safe %s" % (tag, tuple(round(v) for v in box), safe)]
    return []


def _split_with_seps(text, words):
    """把原文按词表切开，保留**词与词之间的原始分隔符**（空格/标点）。

    返回 [(sep, token), ...] + 尾部残余；任何一步在原文里找不到时就返回 None。
    为什么要保留 sep：edge-tts 的中文词表不含空格，但剧本里可能有
    "用 DeepSeek 生成"这种中英混排——直接拼 token 会把空格吃成 "用DeepSeek生成"。
    """
    s = str(text or "")
    toks = [str(w.get("text") or "") for w in (words or [])]
    toks = [t for t in toks if t]
    if not toks:
        return None
    out, pos = [], 0
    for tk in toks:
        i = s.find(tk, pos)
        if i < 0:
            return None
        out.append((s[pos:i], tk))
        pos = i + len(tk)
    return out, s[pos:]


def wrap_by_words(text, f, max_w, words):
    """按**真实词边界**折行。词表对不上原文时返回 None（调用方回退字符折行）。

    为什么值得：中文按字符折行会在一句话的中间硬切（"…不是读/稿"），
    而词边界是 TTS 自己切的分词结果，折出来的断点读起来自然，也更容易和
    词级高亮/字幕窗口对齐。
    """
    got = _split_with_seps(text, words)
    if not got:
        return None
    pairs, tail = got
    if tail.strip():                    # 词表没覆盖整句：不对齐，交回字符折行
        return None
    lines, cur = [], ""
    for sep, tok in pairs:
        trial = cur + sep + tok
        if cur and text_w(trial, f) > max_w:
            lines.append(cur)
            cur = tok.lstrip()
        else:
            cur = trial
    if tail:
        cur += tail
    if cur:
        lines.append(cur)
    return lines or None


def fit_sub_lines(text: str, max_lines=2, max_w=1560, size=46, weight="reg",
                  family="sans", words=None):
    """字幕折行：优先两行，超宽逐级缩字号。返回 (lines, font)。

    传 words（句内词表）时优先按词边界折行；词表对不上原文、或折出来
    仍超过 max_lines 时，**回退到字符折行**——不带词表的排版逐像素不变。
    """
    for sz in (size, int(size * 0.92), int(size * 0.84), int(size * 0.76)):
        f = font(sz, weight, family)
        lines = None
        if words:
            lines = wrap_by_words(text, f, max_w, words)
            if lines is not None and (len(lines) > max_lines
                                      or any(text_w(l, f) > max_w for l in lines)):
                # 两种情况都退回字符折行：①词边界折出来超过 max_lines；
                # ②某个"词"本身就比 max_w 宽（wrap_by_words 对单个超宽 token 不会
                # 断开，会把它当一整行放出去）。②不兜底时字幕横向溢出安全区，而
                # qa_text_safe 用字符折行量宽度、看不出问题——门禁和画面不是同一套折法。
                lines = None
        if lines is None:
            lines = wrap_cn(text, f, max_w)
        if len(lines) <= max_lines:
            return lines, f
    f = font(int(size * 0.76), weight, family)
    return wrap_cn(text, f, max_w), f


def subtitle_kwargs(plan):
    """从已解析的计划里取字幕样式参数。

    返回可直接 ``draw_subtitle(..., **kw)`` 的 dict；计划里没有 subtitle 时返回空 dict
    （= 模块默认值 = 最朴素的字幕带，逐像素不变）。

    为什么放在 filmkit 而不是 render：**参考帧渲染路径**（qavmaf 的 _draw_ref_frame）
    与主渲染路径必须用同一份字幕参数，否则同源的两帧字幕字号不同，VMAF 会被
    系统性拉低（实测：dv 变的 46px 默认 vs 52px 设计，同源参考帧 VMAF 91.3，
    换了正确参数后回到 96+）。一份实现放这里，两条路径都调它。
    """
    sub = plan.get("subtitle") if isinstance(plan, dict) else None
    if not isinstance(sub, dict):
        return {}
    kw = {}
    if sub.get("size") is not None:
        kw["size"] = int(sub["size"])
    if sub.get("y") is not None:
        kw["y"] = int(sub["y"])
    if sub.get("align") in ("center", "left"):
        kw["align"] = sub["align"]
    if sub.get("plate_alpha") is not None:
        kw["plate_alpha"] = int(sub["plate_alpha"])
    if sub.get("radius") is not None:
        kw["radius"] = int(sub["radius"])
    if sub.get("color") is not None:
        kw["color"] = sub["color"]
    if sub.get("max_lines") is not None:
        kw["max_lines"] = int(sub["max_lines"])
    if sub.get("family"):
        kw["family"] = sub["family"]
    return kw

def draw_subtitle(img, text: str, alpha=1.0, size=46, y=None, band_w=None,
                  theme=None, family=None, words=None, align="center",
                  plate_alpha=None, radius=10, color=None, max_lines=2):
    """烧字幕。theme（stylekit 主题）与 family（字体族）可选：
    theme=None 时用内置墨色/黑背板，逐像素可控。words 传句内词表时按词边界折行。

    align / plate_alpha / radius / color / max_lines 全部可选，
    默认值 = 最朴素的字幕带，所以不带这些参数的调用逐像素不变。为什么是参数：
    字幕样式若是**模块常量**，任何两条片子的字幕带都一模一样 —— 这正是"大面积重复"。
    这些参数由 design.json 的 visual.subtitle 决定（见 designkit.subtitle_of）。
    """
    if not text or alpha <= 0.01:
        return img
    if band_w is None:
        band_w = SUB_BAND[2] - SUB_BAND[0]
    th = theme if isinstance(theme, dict) else None
    fam = family or (th or {}).get("family") or "sans"
    try:
        max_lines = max(1, min(3, int(max_lines)))
    except (TypeError, ValueError):
        max_lines = 2
    lines, f = fit_sub_lines(text, max_lines=max_lines, max_w=band_w, size=size,
                             family=fam, words=words)
    lines = lines[:max_lines]
    lh = int(f.size * 1.32)
    total_h = lh * len(lines)
    if align == "left":
        cx = SUB_BAND[0] + 34
    else:
        cx = (SUB_BAND[0] + SUB_BAND[2]) // 2
    base_y = SUB_BAND[3] - total_h if y is None else y
    base_y = max(SUB_BAND[1], min(base_y, SUB_BAND[3] - total_h))
    d = ImageDraw.Draw(img, "RGBA")
    widest = max(text_w(l, f) for l in lines)
    if align == "left":
        bx0 = cx - 34
        bx1 = cx + widest + 34
    else:
        bx0 = cx - widest / 2 - 34
        bx1 = cx + widest / 2 + 34
    by0 = base_y - 16
    by1 = base_y + total_h + 16
    if color is not None:
        ink_rgb = tuple(int(x) for x in color)[:3]
    elif th is None:
        ink_rgb = (245, 248, 252)
    else:
        ink_rgb = tuple(th["ink"])
    ink = tuple(ink_rgb) + (int(255 * alpha),)
    if th is None:
        _pa = 150 if plate_alpha is None else int(plate_alpha)
        plate = (0, 0, 0, int(_pa * alpha))
    else:
        # 浅色主题不能"黑底白字"：背板取 bg2 半透，字取 ink
        _pa = 170 if plate_alpha is None else int(plate_alpha)
        plate = tuple(th["bg2"]) + (int(_pa * alpha),)
    if plate[3] > 0:
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(radius), fill=plate)
    for i, ln in enumerate(lines):
        lw = text_w(ln, f)
        x = (cx - 34 + 34) if align == "left" else (cx - lw / 2)
        d.text((x, base_y + i * lh), ln, font=f, fill=ink)
    return img


# ---------------------------------------------------------------- WAV / PCM
def wav_write(path, samples, sr=SR, channels=None):
    """写 16-bit PCM WAV。channels=None 时按数组形状自动判断（二维 = 立体声）。

    支持立体声（配乐/音效要声像）；wav_write(p, mono) 这类单声道调用行为不变。
    """
    a = np.asarray(samples, dtype=np.float32)
    # 非有限值必须报错：np.clip 会保留 NaN/Inf，astype(int16) 把 NaN 静默写成 0、
    # 把 Inf 写成 undefined，wav 里出现"随机哑音"且函数还返回 nan 峰值（下游只看得到 no）。
    if a.size and not np.all(np.isfinite(a)):
        bad = int(a.size - np.count_nonzero(np.isfinite(a)))
        raise ValueError("wav_write: 采样里有 %d 个 NaN/Inf（%s）——上游混音/合成有数值错误，"
                         "拒绝写出损坏的 WAV" % (bad, path))
    if a.ndim == 2:
        n_ch = a.shape[1]
        if channels is None:
            channels = n_ch
        elif int(channels) != n_ch:
            raise ValueError("wav_write: 数组有 %d 列，但 channels=%s" % (n_ch, channels))
        a = np.clip(a, -1.0, 1.0)
        inter = a.reshape(-1)                       # 交错排列（WAV 要求）
    else:
        a = np.clip(np.ravel(a), -1.0, 1.0)
        inter = a
        if channels is None:
            channels = 1
    peak = float(np.max(np.abs(inter))) if inter.size else 0.0
    pcm = (inter * 32767.0).astype(np.int16)
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(int(channels))
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return peak


def wav_read(path):
    """读 PCM WAV（16 / 24 / 32-bit），返回 float32 数组：单声道 (n,) / 立体声 (n,2)。

    必须同时吃 16-bit 与 24-bit：musicgen / scorekit 落盘都用 PCM_24（多 8 dB 动态余量），
    而 wav_write 只写 16-bit。
    """
    with wave.open(str(path), "rb") as w:
        n_ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)).astype(np.int32)
        v = np.where(v >= 0x800000, v - 0x1000000, v)          # 24-bit 符号扩展
        a = (v.astype(np.float32) / 8388607.0)
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483647.0
    else:
        raise ValueError("wav_read 只支持 16/24/32-bit PCM（%s 是 %d 字节）" % (path, sw))
    if n_ch > 1:
        a = a.reshape(-1, n_ch)
    return a, sr


def decode_pcm(path, sr=SR, channels=1):
    """解码成 float32 PCM：channels=1 -> (n,)，channels=2 -> (n,2)。"""
    ch = max(1, int(channels))
    p = _run([FFMPEG, "-v", "error", "-i", str(path), "-f", "f32le",
              "-ac", str(ch), "-ar", str(sr), "-"])
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("解码失败 %s: %s" % (path, p.stderr.decode("utf-8", "replace")[:300]))
    a = np.frombuffer(p.stdout, dtype=np.float32).copy()
    if ch > 1:
        a = a[: (len(a) // ch) * ch].reshape(-1, ch)
    return a


def audio_dur(path, sr=SR):
    return len(decode_pcm(path, sr)) / float(sr)


def wav_dur(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def trim_silence(pcm, sr=SR, thresh_db=-42.0, keep_ms=80):
    """裁掉首尾静音（edge-tts 尾部常带约 0.95s 静音；55 句累计 61.5s 空白）。"""
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.size == 0:
        return pcm
    th = 10 ** (thresh_db / 20.0)
    idx = np.where(np.abs(pcm) > th)[0]
    if idx.size == 0:
        return pcm
    keep = int(sr * keep_ms / 1000.0)
    a = max(0, int(idx[0]) - keep)
    b = min(len(pcm), int(idx[-1]) + keep)
    return pcm[a:b]


def trim_silence_edges(pcm, sr=SR, thresh_db=-42.0, keep_ms=80):
    """兼容别名。"""
    return trim_silence(pcm, sr=sr, thresh_db=thresh_db, keep_ms=keep_ms)


# ---------------------------------------------------------------- 音频总线
class AudioBus:
    """采样级音频总线：确定性、1/SR 精度、可单层重放、可中途打印峰值。

    channels=1 为单声道（默认）；channels=2 时每个来源自动展开成
    立体声：单声道来源用 pan 定位，立体声来源原样保留。配乐/音效要做声像时必须用 2。
    """

    def __init__(self, total, sr=SR, channels=1):
        self.sr = int(sr)
        self.total = float(total)
        self.channels = max(1, int(channels))
        n = int(round(self.total * self.sr)) + self.sr
        self.bus = np.zeros(n if self.channels == 1 else (n, self.channels), dtype=np.float32)
        self.log = []

    def _fit(self, src):
        """把任意来源变成总线的采样形状（单声道 / 立体声）。"""
        s = np.asarray(src, dtype=np.float32)
        if s.ndim == 0:
            s = s.reshape(1)
        if self.channels == 1:
            return s if s.ndim == 1 else s.mean(axis=1)
        if s.ndim == 1:
            return np.stack([s, s], axis=1)      # 单声道来源：等功率复制，保持能量
        if s.shape[1] == 1:
            return np.concatenate([s, s], axis=1)
        if s.shape[1] > 2:
            return s[:, :2]
        return s

    def place(self, src, at, gain=1.0, loop=False, fade_in=0.0, fade_out=0.0, pan_pos=None):
        if src is None or len(src) == 0:
            return self
        i0 = int(round(float(at) * self.sr))
        if i0 >= len(self.bus):
            return self
        s = np.asarray(src, dtype=np.float32)
        if i0 < 0:
            # 负起点 = 从 0 秒开始、裁掉前面 |i0| 个采样（与 sfxkit._place 同口径）。
            # 把负 i0 直接当负下标切片会让 n 变成 0、随后广播崩（place(src, -0.25) 必炸）。
            if i0 <= -len(s):
                return self
            s, i0 = s[-i0:], 0
        if pan_pos is not None and self.channels == 2 and s.ndim == 1:
            theta = (float(np.clip(pan_pos, -1.0, 1.0)) + 1.0) * np.pi / 4.0
            s = np.stack([s * np.cos(theta), s * np.sin(theta)], axis=1)
        s = np.array(self._fit(s), dtype=np.float32, copy=True)
        if fade_in > 0:
            n = min(len(s), max(1, int(fade_in * self.sr)))
            g = np.linspace(0, 1, n, dtype=np.float32)
            s[:n] *= g if s.ndim == 1 else g[:, None]
        if fade_out > 0:
            n = min(len(s), max(1, int(fade_out * self.sr)))
            g = np.linspace(1, 0, n, dtype=np.float32)
            s[len(s) - n:] *= g if s.ndim == 1 else g[:, None]
        if loop and i0 + len(s) < len(self.bus):
            reps = int(np.ceil((len(self.bus) - i0) / max(1, len(s))))
            s = np.tile(s, (reps, 1)) if s.ndim == 2 else np.tile(s, reps)
        n = min(len(s), len(self.bus) - i0)
        self.bus[i0:i0 + n] += s[:n] * gain
        self.log.append(dict(kind="place", at=round(float(at), 3),
                             dur=round(n / self.sr, 3), gain=gain))
        return self

    def music_bed(self, chords, seg=4.0, gain=0.9, amp=0.055, xfade=1.2, start=0.0):
        """连续配乐床：和弦按 seg 秒推进 + 交叉淡化，不做硬拼接（避免接缝爆点）。"""
        if not chords:
            return self
        t = float(start)
        idx = 0
        if not (float(seg) > 0.0):
            # seg=0 时 t 不前进 -> while 变成死循环（100000 次上限只是"慢死"，
            # 6 秒片实测 >2 分钟）。schema 只约束 seg>=0，必须在入口拒绝。
            raise ValueError("music_bed: seg 必须 > 0，收到 %r（和弦床的段长，默认 4.0）"
                             % (seg,))
        while t < self.total:
            freqs = chords[idx % len(chords)]
            dur = min(seg + xfade, max(0.25, self.total - t + xfade))
            pcm = pad_chord(freqs, dur, amp=amp)
            self.place(pcm, t, gain=gain, fade_in=xfade * 0.6, fade_out=xfade)
            t += seg
            idx += 1
            if idx > 100000:
                break
        return self

    def mix(self, normalize=True, fade=0.6, peak_guard=0.97, verbose=True):
        out = self.bus[:int(round(self.total * self.sr))].copy()
        if out.size == 0:
            return out
        raw_peak = float(np.max(np.abs(out)))
        if fade > 0:
            n = min(len(out), max(1, int(fade * self.sr)))
            g_in = np.linspace(0, 1, n, dtype=np.float32)
            g_out = np.linspace(1, 0, n, dtype=np.float32)
            if out.ndim == 1:
                out[:n] *= g_in
                out[-n:] *= g_out
            else:
                out[:n] *= g_in[:, None]
                out[-n:] *= g_out[:, None]
        if normalize and raw_peak > 1e-6:
            out = out / raw_peak * 0.89
        out = np.tanh(out * 1.02).astype(np.float32) * 0.98
        final_peak = float(np.max(np.abs(out)))
        if verbose:
            print("   混音: 叠加峰值 %.2f -> 输出峰值 %.2f (归一化=%s, %d 层)"
                  % (raw_peak, final_peak, normalize, len(self.log)), flush=True)
        if final_peak > peak_guard and verbose:
            print("   [WARN] 混音峰值 %.3f 接近满刻度，建议降低 voice_gain / music.gain" % final_peak,
                  flush=True)
        return out


# ---------------------------------------------------------------- 提示点
# 画面在"该响一下"的地方登记一个提示点，音频阶段按提示点合成音效轨。
# 这样声音跟着画面走，不需要人肉对时间；换 BPM/改停留窗口后，提示点仍然对得上。

CUE_BASE_DB = {
    "ui_blip": -10, "ui_click": -10, "relay_click": -8, "typing": -10, "typing_at": -10,
    "text_stream": -13, "data_chirp": -12, "whoosh": -13, "impact": -5, "sub_drop": -8,
    "riser": -9, "reverse_swell": -9, "glitch": -10, "tape_rewind": -6, "worldline_shift": -9,
    "wind": -12, "rain": -12, "campfire": -12, "go_stone": -8, "roulette_tick": -10,
    "answer_chime": -10, "knock_wood": -8, "metal_hit": -8, "glass_clink": -10,
}


def cue_time(at, scenes=None, total=None):
    """把提示点的 at 解析成成片绝对秒。

    支持四种写法（CONTRACT.md §4.1）：
      "abs:12.5" / 12.5      -> 绝对秒
      "l03+0.35" / "l03-0.2" -> 场景 id 加/减偏移（相对场景 start）
      "l03"                  -> 场景起点
      "l03:w2" / "l03:w2+0.1" -> **词锚点**：该句第 2 个词的起点 + 偏移。
        下标从 1 开始（w1 = 第一个词）；找不到该词时**退化为句偏移**并告警，
        不让一个写错的锚点把整条 cues 链路炸掉（与"跳过并留痕"同一口径）。
    """
    if isinstance(at, (int, float)):
        return round(float(at), 3)
    s = str(at).strip()
    if not s:
        raise ValueError("cue at 为空")
    if s.startswith("abs:"):
        return round(float(s[4:]), 3)
    try:
        return round(float(s), 3)
    except ValueError:
        pass
    # 词锚点：<id>:w<N>[+/-<offset>]
    if ":w" in s:
        head, _sep, tail = s.partition(":w")
        # tail 形如 "2" / "2+0.1" / "2-0.1"：词序号 + 可选偏移（偏移可带符号）
        m = re.match(r"^([0-9]+)(.*)$", tail)
        if m:
            num = m.group(1)
            rest = m.group(2) or ""
            off = 0.0
            if rest:
                try:
                    off = float(rest)
                except ValueError:
                    off = 0.0
            if scenes:
                for sc in scenes:
                    if str(sc.get("id")) != head:
                        continue
                    ws = sc.get("words_abs") or sc.get("words") or []
                    idx = int(num) - 1
                    if 0 <= idx < len(ws):
                        return round(float(ws[idx]["start"]) + off, 3)
                    print("   [WARN] cue at=%r：%s 只有 %d 个词，w%s 不存在，退化为句偏移"
                          % (at, head, len(ws), num), flush=True)
                    return round(float(sc["start"]) + off, 3)
            raise ValueError("cue at=%r：找不到场景 %r（先跑 layout，或改用 abs:秒）" % (at, head))
    # 形如 <id>[+-]<offset>
    cut = len(s)
    for i, ch in enumerate(s):
        if ch in "+-" and i > 0:
            cut = i
            break
    sid = s[:cut]
    off = float(s[cut:] or 0.0) if cut < len(s) else 0.0
    if scenes:
        for sc in scenes:
            if str(sc.get("id")) == sid:
                return round(float(sc["start"]) + off, 3)
    raise ValueError("cue at=%r：找不到场景 %r（先跑 layout，或改用 abs:秒）" % (at, sid))


def scenes_anchors(scenes):
    """给配乐引擎分段用的锚点：[{id, kind, start, end}]。"""
    return [dict(id=s.get("id"), kind=s.get("kind") or "body",
                 start=float(s["start"]), end=float(s["end"])) for s in scenes]


def content_seed(base: int, style: str, content_fp: str, recipe: str, t: float) -> int:
    """内容派生 seed：同影片同 cue 稳定，不同影片同一位置也换音色细节。

    公式与 sfxkit.content_seed 一致（references/audio-variation.md §3.1）：
        sha1("v0.7|<base>|<style>|<content_fp>|<recipe>|<t 三位小数>") -> 前 8 位 hex -> % 9000 + 1000

    为什么不用 seed_base + i：那样只跟 cue 的**出现顺序**有关，两部内容完全不同的片子
    只要 cues 一样，音效轨就逐样本相同（跨片"音效都一样"的直接来源）。
    """
    h = hashlib.sha1(
        ("v0.7|%d|%s|%s|%s|%.3f" % (int(base), str(style), str(content_fp), str(recipe), float(t)))
        .encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 9000 + 1000


class CueBus:
    """提示点登记表：画面侧登记 -> 落盘 cues.json -> 音频侧按它合成音效。

    film.json 的 sfx.cues 与 scenes.py 的可选 cues() 钩子统一走这里；
    浏览器引擎则由页面 window.getCues() 导出同 schema 的 JSON（CONTRACT.md §4.2）。
    """

    def __init__(self, scenes=None, total=None):
        self.scenes = scenes or []
        self.total = None if total is None else float(total)
        self.items = []
        self._drop_warned = set()   # 已告警过的丢弃项：同一实例只报一次（见 resolved）

    # -- 登记 --
    def add(self, at, type, **params):
        t = cue_time(at, self.scenes, self.total)
        kind = CUE_ALIASES.get(str(type), str(type))
        row = dict(params)
        row.update(t=round(t, 3), type=kind)
        if "src" not in row:
            row["src"] = "pil"
        self.items.append(row)
        return self

    def extend(self, items):
        for it in items or []:
            if not it:
                continue
            row = dict(it)
            at = row.pop("at", row.pop("t", None))
            if at is None:
                raise ValueError("提示点缺少 at/t：%r" % (it,))
            self.add(at, row.pop("type"), **row)
        return self

    # -- 解析 --
    def resolved(self):
        """按时间排序、去掉明显无效项（超过片长 2 秒 / 负时刻），并**真的**告警。

        为什么必须告警：st_sfx 只把 build_audio 的产物当音效轨，被丢掉的 cue 不会进
        任何报告；静默 continue（文档却写着"丢弃并告警"）时少一个提示音，
        用户只能靠听。st_sfx 会调两次 resolved()（自己一次 + build_audio 内部一次），
        所以只在**第一次**发现丢弃时打一条汇总，避免同一件事刷两遍。
        """
        out = []
        dropped = []
        for row in self.items:
            t = float(row["t"])
            if t < -0.05:
                dropped.append(("负时刻", t))
                continue
            if self.total is not None and t > self.total + 2.0:
                dropped.append(("超出片长", t))
                continue
            out.append(row)
        fresh = [d for d in dropped if d not in self._drop_warned]
        if fresh:
            self._drop_warned.update(fresh)
            late = [x for x in fresh if x[0] == "超出片长"]
            neg = [x for x in fresh if x[0] == "负时刻"]
            parts = []
            if late:
                parts.append("%d 条超过片长(%.2fs)+2s" % (len(late), float(self.total)))
            if neg:
                parts.append("%d 条时刻为负" % len(neg))
            print("   [WARN] CueBus: 丢弃 %d 条提示点（%s）——检查 cue 的 at 写法/场景 id"
                  % (len(fresh), "；".join(parts)), flush=True)
        return sorted(out, key=lambda r: float(r["t"]))

    def to_json(self, duration=None, fps=None, width=None, height=None, events=None):
        dur = self.total if duration is None else float(duration)
        return dict(version=1, duration=round(dur, 3),
                    fps=int(FPS if fps is None else fps),
                    width=int(W if width is None else width),
                    height=int(H if height is None else height),
                    cues=self.resolved(), events=list(events or []))

    def write(self, path, duration=None, events=None, **kw):
        payload = self.to_json(duration=duration, events=events, **kw)
        d = os.path.dirname(os.path.abspath(str(path)))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(str(path), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        return payload

    @classmethod
    def from_json(cls, path):
        with open(str(path), encoding="utf-8") as fh:
            d = json.load(fh)
        bus = cls(total=d.get("duration"))
        for c in d.get("cues") or []:
            row = dict(c)
            row.setdefault("src", "file")
            bus.items.append(row)
        return bus

    # -- 合成 --
    def build_audio(self, total=None, sr=SR, base_db=None, default_gain_db=0.0,
                    seed_base=1000, verbose=True,
                    style=None, content_fp=None, key=None):
        """按提示点合成音效轨 -> (n,2) float32。

        依赖 scripts/sfxkit.py。缺 sfxkit 时直接报错，不静默出哑片。
        未知提示点类型：告警一次并跳过（不 crash 整片混音）。
        提示点参数非法（配方不认识的关键字 / 取值连枚举校验都过不了，TypeError 或
        ValueError）：该条跳过并汇总 [WARN]，其余正常落位——不静默改渲染默认音色，
        也不因一条 cue 写错丢整条音效轨。

        音频差异化（见 references/audio-variation.md）：
          - style / content_fp 同时给时，每条 cue 的 seed 由
            content_seed(seed_base, style, content_fp, type, t) 派生 -> 不同影片同一 cue
            位置也拿到不同音色细节；任一为 None 时退回 seed_base + i（逐样本与基线一致）。
          - key 非空时透传给配方（提示音调性跟随配乐，不硬编码 Dm）。
        """
        try:
            import sfxkit as SX
        except Exception as e:                              # noqa: BLE001
            raise RuntimeError(
                "合成音效轨需要 sfxkit.py（应与 filmkit.py 同目录）：%s" % e) from e
        dur = float(self.total if total is None else total)
        base = dict(CUE_BASE_DB)
        if base_db:
            base.update(base_db)
        reg = SX.load_all() if hasattr(SX, "load_all") else {}
        n = int(round(dur * sr)) + int(sr * 0.5)
        bus = np.zeros((n, 2), dtype=np.float32)
        unknown, placed = set(), 0
        bad_cues: list = []          # 参数非法/内部报错的 cue：跳过并汇总 WARN，不拖死整条音效轨
        keyed = style is not None and content_fp is not None
        if keyed and verbose:
            print("   音效 seed: 内容派生（style=%s / fp=%s）" % (style, str(content_fp)[:8]))
        elif verbose and style is not None:
            print("   音效 seed: 位置派生（只给了 style 没给 content_fp）")
        for i, c in enumerate(self.resolved()):
            kind = str(c.get("type"))
            if kind not in reg:
                unknown.add(kind)
                continue
            params = {k: v for k, v in c.items()
                      if k not in ("t", "type", "src", "gain", "pan", "base_db", "key")}
            params.pop("dur_hint", None)
            if keyed:
                seed = content_seed(seed_base, style, content_fp, kind, float(c["t"]))
            else:
                seed = int(seed_base) + i
            if key is not None:
                params["key"] = key
            try:
                x = SX.render(kind, seed=seed, **params)
            except (TypeError, ValueError) as err:
                # 只对"配方不认识这个关键字"逐个删参重试（兼容严格签名的旧配方）；
                # 参数值非法（cue 里 pitch=None 之类）或配方内部 TypeError 不是签名问题，
                # 必须报告——一路退化成"只传 seed"会把坏参数静默渲染成默认音色
                # （用户改了 cue 参数却听不出变化，日志里一个字都没有）。
                extra = dict(params)
                while True:
                    hit = re.search(r"unexpected keyword argument '([^']+)'", str(err))
                    if not hit or hit.group(1) not in extra:
                        bad_cues.append((kind, float(c["t"]), err))
                        x = None
                        break
                    extra.pop(hit.group(1))
                    try:
                        x = SX.render(kind, seed=seed, **extra)
                        break
                    except (TypeError, ValueError) as err2:
                        err = err2
            if x is None or len(x) == 0:
                continue
            x = np.asarray(x, dtype=np.float32)
            if x.ndim == 1:
                p = c.get("pan")
                theta = ((float(np.clip(p, -1, 1)) if p is not None else 0.0) + 1.0) * np.pi / 4.0
                x = np.stack([x * np.cos(theta), x * np.sin(theta)], axis=1)
            if x.shape[1] > 2:
                x = x[:, :2]
            g = float(base.get(kind, -10.0)) + float(c.get("gain", 0.0) or 0.0) + default_gain_db
            s = int(round(float(c["t"]) * sr))
            if s < 0 or s >= n:
                continue
            e = min(n, s + len(x))
            bus[s:e] += (10.0 ** (g / 20.0)) * x[: e - s]
            placed += 1
        if unknown and verbose:
            print("   [WARN] 未知提示点类型（已跳过）: %s" % ", ".join(sorted(unknown)))
        if bad_cues and verbose:
            # 与"未知 type 跳过"同一条隔离策略：一条 cue 的参数写错不该让整条音效轨消失
            # （st_sfx 会把 build_audio 的异常当成"音效合成失败"整轨丢弃）。但也不能静默：
            # 逐条打印 cue 类型/时刻/原因，用户改完参数能立刻看到。
            print("   [WARN] %d 条提示点参数非法（已跳过，其余正常落位）：" % len(bad_cues))
            for kind_b, t_b, err_b in bad_cues[:6]:
                print("     %-14s t=%.3fs  -> %s" % (kind_b, t_b, str(err_b)[:110]))
        if verbose:
            print("   音效轨：%d 个提示点落位 / %d 个登记（%.2fs）"
                  % (placed, len(self.items), dur))
        return bus[: int(round(dur * sr))]


def voice_env(voice, sr=SR, block_s=0.01, thresh_db=-42.0, floor_db=-60.0):
    """旁白的语音活跃度包络（0~1）：给"旁白优先"混音用的侧链源。

    为什么需要它：配乐基线只比旁白低 1.5 dB（`music.gain_db=-1.5`）时，
    而视频是"以旁白为主"的——背景只要不明显低于人声，观众就得费力分辨字。
    `mix_film` 用这条包络在**有人声的地方**把配乐/音效再压下去，
    没人声的间隙自动让背景回来（否则整片背景像被闷住）。

    实现：10 ms 块级 RMS -> dB -> 在 [floor_db, thresh_db] 之间线性映射成 0~1 ->
    起音快 / 释放慢的单向平滑（与 `duck()` 同一套向量化手法，无逐采样循环）。
    """
    x = np.asarray(voice, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    n = len(x)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    block = max(1, int(sr * float(block_s)))
    nb = n // block
    if nb == 0:                                # 极短旁白（< 10 ms）：整段当一块并补零
        nb = 1                                 # （直接 reshape(1, block) 会崩）
        x = np.concatenate([x, np.zeros(block - n, dtype=np.float32)])
    seg = x[: nb * block].reshape(nb, block)
    rms = np.sqrt(np.mean(seg * seg, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    lo = float(min(floor_db, thresh_db - 1.0))
    e = np.clip((db - lo) / (float(thresh_db) - lo), 0.0, 1.0)

    def _smooth(v, tau_s):
        # 极短旁白时块数可能比核还少：np.convolve(mode="same") 返回 max(len(v), k) 长度，
        # 两个长度不同的平滑结果再做 np.maximum 会广播失败（voice_env 曾崩在这里）。
        k = min(max(1, int(sr * tau_s / block)), len(v))
        w = np.ones(k, dtype=np.float32) / k
        return np.convolve(v, w, mode="same")

    e = np.maximum(_smooth(e, 0.06), _smooth(e, 0.45)).astype(np.float32)
    return np.interp(np.arange(n), np.arange(nb) * block + block / 2.0, e).astype(np.float32)


def duck_voice(track, voice, depth_db=6.0, sr=SR, thresh_db=-42.0, floor_db=-60.0):
    """旁白说话时压低背景（配乐 / 音效）：旁白优先的侧链压缩（只压背景，不碰人声）。

    depth_db 是"人声满电平时背景最多被压多少 dB"；间隙里包络回 0，背景恢复原电平。
    与 `duck()`（音效压配乐）叠加使用：人声 > 音效 > 配乐，三条让位链互不冲突。
    """
    b = np.asarray(track, dtype=np.float32)
    if b.ndim == 1:
        b2 = np.stack([b, b], axis=1)
    else:
        b2 = b
    n = len(b2)
    if n == 0 or depth_db <= 0:
        return b2
    e = voice_env(voice, sr=sr, thresh_db=thresh_db, floor_db=floor_db)[:n]
    if e.size == 0:
        return b2
    if e.size < n:
        # 旁白比背景短（单独调用时可能遇到）：旁白结束之后没有侧链源，背景应原样恢复。
        e = np.concatenate([e, np.zeros(n - e.size, dtype=np.float32)])
    if float(np.max(e)) <= 1e-6:
        return b2
    gains = 10.0 ** (-float(depth_db) * e / 20.0)
    return (b2 * gains[:, None]).astype(np.float32)


def voice_snr(voice, bg, sr=SR, thresh_db=-42.0, floor_db=-60.0):
    """在"有人声"的采样区间量 人声/背景 的响度差（dB）。旁白优先的客观指标。

    只在语音活跃度 >= 0.5 的区间统计：静音间隙里背景当然是唯一声源，
    把间隙算进来会稀释问题（真正让人听不清的，是人说话那一刻背景有多响）。
    人声总长不足 50 ms 时返回 None（没有可判定的样本）。
    """
    v = np.asarray(voice, dtype=np.float64)
    b = np.asarray(bg, dtype=np.float64)
    if v.ndim == 2:
        v = v.mean(axis=1)
    if b.ndim == 2:
        b = b.mean(axis=1)
    n = min(len(v), len(b))
    if n <= 0:
        return None
    v, b = v[:n], b[:n]
    e = voice_env(v, sr=sr, thresh_db=thresh_db, floor_db=floor_db)
    act = e >= 0.5
    if int(np.sum(act)) < int(0.05 * sr):
        return None
    vr = float(np.sqrt(np.mean(v[act] ** 2)))
    br = float(np.sqrt(np.mean(b[act] ** 2)))
    return 20.0 * np.log10((vr + 1e-12) / (br + 1e-12))


def guard_snr(voice, bg, target_db=12.0, cap_db=12.0, sr=SR,
              thresh_db=-42.0, floor_db=-60.0):
    """旁白清晰度兜底：人声/背景不足 target_db 时，返回背景应再压多少 dB。

    返回 (trim_db, snr_db)：trim_db=0 表示已达标；cap_db 防止把背景压到无声
    （压过头会变成"配乐忽有忽无"，比听不清更假）。判定口径同 `voice_snr()`。
    """
    snr = voice_snr(voice, bg, sr=sr, thresh_db=thresh_db, floor_db=floor_db)
    if snr is None or snr >= float(target_db):
        return 0.0, (snr if snr is not None else float("nan"))
    return float(min(max(0.0, float(cap_db)), float(target_db) - snr)), snr


def duck(music, sfx, depth_db=4.0, attack=0.03, release=0.35, sr=SR):
    """音效响起时配乐自动让位（最多 depth_db），避免重击被配乐吃掉。

    用分块包络实现（向量化），不做逐采样循环：块内线性插值出增益曲线。
    """
    m = np.asarray(music, dtype=np.float32)
    s = np.asarray(sfx, dtype=np.float32)
    if m.ndim == 1:
        m2 = np.stack([m, m], axis=1)
    else:
        m2 = m
    env = np.abs(s).max(axis=1) if s.ndim == 2 else np.abs(s)
    n = len(m2)
    env = env[:n]
    if env.size == 0 or float(env.max()) <= 1e-9:
        return m2
    block = max(1, int(sr * 0.01))
    nb = n // block
    if nb == 0:                                 # 极短音效（< 10 ms）：整段当一块并补零
        nb = 1
        env = np.concatenate([env, np.zeros(block - n, dtype=np.float32)])
    e = env[: nb * block].reshape(nb, block).max(axis=1)
    e = e / (float(np.percentile(e, 99.5)) + 1e-9)
    e = np.clip(e, 0.0, 1.0)
    # 单向平滑：起音快、释放慢（对包络做两次不同时间常数的指数平滑，仍保持向量化）
    def _smooth(x, tau_s):
        # 与 voice_env 同一处理：短片段（< release 0.35 s）时核不能比信号长，
        # 否则 np.convolve(mode="same") 返回核长、np.maximum 广播失败（短片混音直接崩）。
        k = min(max(1, int(sr * tau_s / block)), len(x))
        w = np.ones(k, dtype=np.float32) / k
        return np.convolve(x, w, mode="same")
    e = np.maximum(_smooth(e, attack), _smooth(e, release) * 0.7)
    gains = 10.0 ** (-float(depth_db) * np.clip(e, 0, 1) / 20.0)
    curve = np.interp(np.arange(n), np.arange(nb) * block + block / 2.0, gains).astype(np.float32)
    return (m2 * curve[:, None]).astype(np.float32)


def mix_film(voice, music=None, sfx=None, total=None, sr=SR,
             voice_gain=1.0, music_gain_db=0.0, sfx_gain_db=0.0,
             duck_db=3.0, peak_target=0.89, fade=0.4, verbose=True,
             voice_duck_db=6.0, voice_duck_sfx_db=3.0,
             voice_thresh_db=-42.0, voice_floor_db=-60.0,
             guard_snr_db=12.0, guard_cap_db=30.0):
    """三总线混音：旁白 + 配乐 + 音效 -> 立体声 float32。

    **旁白优先**（硬行为，不是文档口号）：
      1. 旁白电平固定 `voice_gain=1.0`，不被任何侧链影响；
      2. 有人声的地方，配乐再压 `voice_duck_db`（默认 6 dB）、音效再压 `voice_duck_sfx_db`（默认 3 dB）
         —— 保证"人声比背景响 ≥12 dB"这条可听清线；没人声的间隙自动恢复；
      3. 音效响时配乐额外让位（`duck_db`，默认 4 dB）；
      4. 最后统一做峰值保护（真正的响度标准化交给 ffmpeg 的 loudnorm）。

    `voice_duck_db=0` / `voice_duck_sfx_db=0` 可单独关掉旁白优先让位（旧片对比用）。

    **SNR 兜底（guard_snr_db=12，0 = 关）**：按台词活跃区实测"人声/背景"响度差，
    不足 12 dB 就整体再压背景（最多 guard_cap_db），保证"听清旁白"不依赖配乐素材的天然电平——
    这是本仓库唯一一条会在混音后自己改电平的门禁。触发时会打印实际 SNR 与补压量。
    """
    def _to_stereo(x):
        a = np.asarray(x, dtype=np.float32)
        if a.ndim == 1:
            return np.stack([a, a], axis=1)
        if a.shape[1] == 1:
            return np.concatenate([a, a], axis=1)
        # 多于 2 声道（例如把 5.1 的 music.wav 塞进 work/）：只取前两声道。
        # 旧实现原样返回 (n,3+)，back += mm 直接广播崩（mix_film 是公共 API，
        # 而 wav_read 不限制声道数，用户完全可能踩到）。口径与 AudioBus._fit 一致。
        return a[:, :2] if a.shape[1] > 2 else a

    n = None
    v = None if voice is None else _to_stereo(voice)
    m = None if music is None else _to_stereo(music)
    s = None if sfx is None else _to_stereo(sfx)
    for x in (v, m, s):
        if x is not None:
            n = len(x) if n is None else min(n, len(x))
    if total is not None:
        want = int(round(float(total) * sr))
        n = want if n is None else min(n, want)
    if not n:
        return np.zeros((0, 2), dtype=np.float32)

    # 旁白优先：先算背景（配乐 + 音效各自让位），再用 SNR 兜底确认"人声压得住"。
    # 侧链与 SNR 都用"实际交付的人声"（乘过 voice_gain）：若用未缩放的
    # 人声算 SNR，voice_gain=0.5 时日志写"人声/背景=12 dB 达标"、交付出去只有 6 dB
    # （兜底承诺失效）。
    venv_kw = dict(sr=sr, thresh_db=float(voice_thresh_db), floor_db=float(voice_floor_db))
    vg = None
    if v is not None:
        vg = v[:n] * float(voice_gain)
    back = np.zeros((n, 2), dtype=np.float32)
    if s is not None:
        ss = s[:n] * (10.0 ** (float(sfx_gain_db) / 20.0))
        if vg is not None and float(voice_duck_sfx_db) > 0:
            ss = duck_voice(ss, vg, depth_db=float(voice_duck_sfx_db), **venv_kw)
        back += ss
    if m is not None:
        mm = m[:n] * (10.0 ** (float(music_gain_db) / 20.0))
        if s is not None:
            mm = duck(mm, s[:n], depth_db=duck_db, sr=sr)
        else:
            mm = _to_stereo(mm)
        if vg is not None and float(voice_duck_db) > 0:
            mm = duck_voice(mm, vg, depth_db=float(voice_duck_db), **venv_kw)
        back += mm
    # SNR 兜底：实测人声活跃区的人声/背景比；不够就整体再压背景（不碰人声）。
    if vg is not None and back.any() and float(guard_snr_db) > 0:
        trim_db, snr_db = guard_snr(vg, back, target_db=float(guard_snr_db),
                                    cap_db=float(guard_cap_db), **venv_kw)
        if trim_db > 0.0:
            back *= 10.0 ** (-trim_db / 20.0)
        if verbose:
            if snr_db == snr_db:                            # 非 NaN = 有可判定的人声段
                note = "，达标"
                if trim_db > 0:
                    note = "，背景再压 %.1f dB" % trim_db
                    if trim_db >= float(guard_cap_db) - 1e-6:
                        note += "（已到兜底上限：检查配乐/音效素材电平）"
                    elif trim_db > 18.0:
                        note += "（补压很大：建议调低 music.gain_db）"
                print("   旁白优先: 人声/背景 = %.1f dB（目标 %.1f）%s"
                      % (snr_db, float(guard_snr_db), note))
            else:
                # snr_db 是 NaN = 活跃区为空：旁白电平低于 voice_thresh_db 时，旁白优先
                # 与 SNR 兜底会**整体静默失效**（背景一点不压）。纯静音片是有意的，
                # 但"目的是说话却录得很轻"必须在日志里喊出来，否则等于没有门禁。
                vpk = float(np.max(np.abs(vg))) if vg.size else 0.0
                if vpk > 1e-4:                                   # 有声音但到不了活跃区
                    print("   [WARN] 旁白电平过低（峰值 %.1f dBFS，低于活跃度门限 %.0f dB）——"
                          "旁白优先/SNR 兜底本次无法判定；检查 voice_gain / TTS 音量"
                          % (20.0 * math.log10(vpk), float(voice_thresh_db)))
    out = back
    if v is not None:
        out = out + v[:n] * float(voice_gain)

    raw = float(np.max(np.abs(out))) if out.size else 0.0
    if fade > 0:
        k = min(n, max(1, int(fade * sr)))
        g_in = np.linspace(0, 1, k, dtype=np.float32)[:, None]
        g_out = np.linspace(1, 0, k, dtype=np.float32)[:, None]
        out[:k] *= g_in
        out[-k:] *= g_out
    if raw > 1e-9 and raw > peak_target:
        out *= peak_target / raw
    if verbose:
        print("   混音: 叠加峰值 %.2f -> 输出峰值 %.3f（旁白%s 配乐%s 音效%s）"
              % (raw, float(np.max(np.abs(out))) if out.size else 0.0,
                 "有" if v is not None else "无", "有" if m is not None else "无",
                 "有" if s is not None else "无"))
    return out


# ---------------------------------------------------------------- 配乐来源判定
MUSIC_PLANS = ("score", "file", "bed", "off", "none", "missing-file", "missing-bpm", "unknown-mode")


def music_plan(script, has_music_file: bool = False):
    """统一判定"这次配乐从哪来"，返回 (plan, 说明)。

    为什么要一个统一函数：若 st_score 说"mode=off 不生成配乐"、而 st_audio
    又无条件读已存在的 work/music.wav —— 用户把 mode 改成 off / 删掉 bpm 之后，
    上一轮跑出来的旧配乐仍被混进成片（语义上 off = 不要配乐）。
    score 与 audio 两个 stage 都用这一份判定，口径不会打架。

    plan 取值：
      score         现场编排配乐（musicgen -> work/music.wav）；scorekit 仅剩 legacy 原语
      file          外部提供 / 上一轮留下的 work/music.wav
      bed           和弦床（music.chords）
      off           明确不要配乐（mode=off/none 或 music.enabled=false）
      none          没配 bpm / chords / music.wav，本次没有配乐
      missing-file  mode=file 但没有 work/music.wav（调用方应明确报错）
      missing-bpm   mode=score 但没有 bpm（调用方应明确报错）
      unknown-mode  music.mode 写了不认识的值（调用方应明确报错）

    兼容语义（不写 music.mode）：
      有 bpm -> score；没 bpm 但有外部 music.wav -> file；再退和弦床 -> bed。
    """
    mus = script.get("music") or {}
    raw = str(mus.get("mode") or "").strip().lower()
    # 设计路径的配乐参数在 script["_style"]["music"] 里（designkit 生成），
    # 而 film.json 顶层通常**没有** music 块。只看顶层的话，"有设计但没写 music"
    # 会被判成 none（本片无配乐）—— 实测设计里 bpm=122/mood=driving，配乐却整段没出。
    # 所以顶层与设计计划合并：显式写 music 的字段优先（用户能覆盖设计），没写的用设计值。
    plan_mus = ((script.get("_style") or {}).get("music") or {})
    if plan_mus:
        merged = dict(plan_mus)
        merged.update({k: v for k, v in mus.items() if v is not None})
        mus = merged
        if not raw:
            raw = str(mus.get("mode") or "params").strip().lower()
            if raw == "params":
                raw = ""            # params == 默认"按参数现场生成"
    # bpm 可以是 "auto"（由风格梯度和内容指纹决定）。
    # 这里只判断"有没有 bpm 这个概念"，具体数值由 st_score 传给配乐引擎。
    bpm = mus.get("bpm", script.get("bpm"))
    if raw == "auto":
        raw = ""
    if raw in ("off", "none", "mute"):
        return "off", "music.mode=%s：本片不要配乐（不读 work/music.wav）" % raw
    if not raw and not mus.get("enabled", True):
        return "off", "music.enabled=false：本片不要配乐（不读 work/music.wav）"
    if raw in ("bed", "chord", "chords"):
        return "bed", "music.mode=bed：和弦床（不读 work/music.wav）"
    if raw in ("file", "external", "provided"):
        if has_music_file:
            return "file", "music.mode=file：外部 work/music.wav"
        return "missing-file", ("music.mode=file 但 work/music.wav 不存在"
                                "（把外部配乐拷成 work/music.wav，或删掉 mode=file）")
    if raw in ("score", "programmatic"):
        if bpm in (None, ""):
            return "missing-bpm", "music.mode=score 但没有 bpm（配乐引擎需要 bpm）"
        return "score", "music.mode=score：现场编排配乐 bpm=%s" % bpm
    if raw:
        return "unknown-mode", ("music.mode=%r 不认识（可用：score / bed / file / off，"
                                "或不写 mode 走兼容链）" % mus.get("mode"))
    # 缺省（兼容链）
    if bpm not in (None, ""):
        return "score", "缺省配乐：有 bpm=%s，现场编排（musicgen）" % bpm
    if has_music_file:
        return "file", "缺省配乐：沿用已有 work/music.wav（外部提供的配乐）"
    if mus.get("chords"):
        return "bed", "缺省配乐：没有 bpm，退回和弦床"
    return "none", "缺省配乐：没有 bpm / music.chords / work/music.wav，本片无配乐"


# ---------------------------------------------------------------- 风格 / 主题
def _stylekit():
    """惰性 import stylekit（同目录；缺了就明确报错，而不是静默回落到单一样式）。"""
    try:
        import stylekit as ST
        return ST
    except Exception as e:                                  # noqa: BLE001
        raise RuntimeError("找不到 stylekit.py（scenes/render 都依赖它，"
                           "请与 filmkit.py 放在同一目录）：%s" % e) from e


def resolve_theme(script, scene=None):
    """给场景取主题字典。

    优先级：scene["theme"]（同明暗族才允许） > script 里解析出的风格主题。
    没有任何计划就报错并给两步指引（fail-closed，不回落内置外观）。
    """
    plan = script.get("_style") if isinstance(script, dict) else None
    if not isinstance(plan, dict):
        raise ValueError(
            "resolve_theme：剧本里没有已解析的设计计划（script[_style]）。\n"
            "  本技能不提供任何内置主题：每一版外观都必须现场生成并由人确认。\n"
            "  请用 python propose.py --brief \"题材\" --script <film.json> --n 3，"
            "\n  再 --confirm --candidate A 冻结设计；渲染侧走 render.resolve_style(script, work)。")
    # 设计路径的计划自带完整主题；没有内置主题表可回查。
    th = plan.get("theme")
    if not isinstance(th, dict) or not th:
        raise ValueError("resolve_theme：计划里没有 theme 字段（plan 来自 %r）——"
                         "它不是 designkit/render 产出的计划，取不到主题"
                         % (plan.get("generator") or plan.get("style"),))
    # scene["theme"] 覆盖（"这一段换一套命名主题"）已随内置主题表一起消失：
    # 可选装饰字段，静默忽略（它不是契约字段，为此报错会让老项目无法渲染）。
    return dict(th)


def style_plan(script):
    """取已解析的风格计划；**不做 legacy 回落**（fail-closed）。

    不能 except 之后返回一份手写的 legacy 计划（120BPM / Dm / 固定节奏）：
    那等于把"没有设计"伪装成"老项目正常出片"，也是模板残留（本技能不提供任何模板）。
    只认一种情况：script["_style"] 已经是计划（render.resolve_style 挂好的）；
    否则明确报错并给两步迁移指引。
    """
    plan = script.get("_style") if isinstance(script, dict) else None
    if isinstance(plan, dict):
        return plan
    raise ValueError(
        "style_plan：剧本里没有已解析的风格计划（script[_style]）。\n"
        "  没有「不写设计就走老外观」这条兼容路径。\n"
        "  正确用法：\n"
        "    python propose.py --brief \"题材\" --script <film.json> --n 3\n"
        "    python propose.py --confirm --candidate A\n"
        "    python render.py --script <film.json> --stage all")


# ---------------------------------------------------------------- 程序化音源
def tone(freq, dur, sr=SR, amp=0.2, kind="sine", decay=2.2, harmonics=(1.0,)):
    """单音衰减。kind 参数保留以兼容老调用（目前只实现 sine）。"""
    n = max(1, int(dur * sr))
    t = np.arange(n) / sr
    out = np.zeros(n, dtype=np.float32)
    if kind == "sine":
        for k, a in enumerate(harmonics, start=1):
            out += (a / k) * np.sin(2 * np.pi * freq * k * t)
    else:                                   # 简单三角近似，避免旧调用直接抛错
        for k, a in enumerate(harmonics, start=1):
            out += (a / k) * (2 / np.pi) * np.arcsin(np.sin(2 * np.pi * freq * k * t))
    return (amp * out * np.exp(-decay * t).astype(np.float32)).astype(np.float32)


def pad_chord(freqs, dur, sr=SR, amp=0.05, detune=0.004):
    n = max(1, int(dur * sr))
    t = np.arange(n) / sr
    out = np.zeros(n, dtype=np.float32)
    for f in freqs:
        for d in (-detune, 0.0, detune):
            out += amp * np.sin(2 * np.pi * f * (1 + d) * t)
    a = min(int(0.9 * sr), n)
    r = min(int(1.2 * sr), n)
    env = np.ones(n, dtype=np.float32)
    if a > 1:
        env[:a] = np.linspace(0, 1, a)
    if r > 1:
        env[-r:] *= np.linspace(1, 0, r)
    return (out * env / max(1, len(freqs))).astype(np.float32)


def noise_burst(dur, sr=SR, amp=0.1, lo=0.02, hi=0.35, decay=6.0, seed=7):
    """噪声脉冲。lo/hi 参数保留以兼容老调用（当前用低通近似，不严格按频段切）。"""
    n = max(1, int(dur * sr))
    r = np.random.RandomState(seed)
    w = r.randn(n).astype(np.float32)
    k = max(1, int(sr / 2000))
    w = np.convolve(w, np.ones(k, dtype=np.float32) / k, mode="same")
    t = np.arange(n) / sr
    return (amp * w * np.exp(-decay * t)).astype(np.float32)


def whoosh(dur=0.45, sr=SR, amp=0.06, seed=3):
    n = max(1, int(dur * sr))
    r = np.random.RandomState(seed)
    w = r.randn(n).astype(np.float32)
    k = max(1, int(sr / 1800))
    w = np.convolve(w, np.ones(k, dtype=np.float32) / k, mode="same")
    t = np.linspace(0, 1, n, dtype=np.float32)
    return (amp * w * np.sin(np.pi * np.power(t, 0.7)) ** 2).astype(np.float32)


# ---------------------------------------------------------------- TTS
def tts_key(text, voice, rate, pitch) -> str:
    """v1 缓存键：4 参签名保持不变（selfcheck 与用户脚本都在用）。"""
    h = hashlib.sha1(("%s|%s|%s|%s" % (text, voice, rate, pitch)).encode("utf-8"))
    return h.hexdigest()[:16]


def tts_key_v2(text, voice, rate, pitch, volume="+0%", trim=True, trim_db=-42.0,
               keep_ms=None) -> str:
    """v2 缓存键：把 volume / trim 开关也纳入。

    为什么必须升 v2：trim 不进键时，用户把 trim_silence 改成 false 之后，
    老 meta 里 trimmed=True 仍会命中第一条分支，返回**裁剪版**——"关了裁剪其实没关"。
    """
    payload = "%s|%s|%s|%s|%s|%s|%s|%s" % ("v2", text, voice, rate, pitch,
                                           volume, int(bool(trim)), round(float(trim_db), 3))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- TTS 后端
# 为什么要有这一层：整条旁白链路只挂在一个**非官方**客户端（edge-tts）上。
# 它的服务端随时可能改协议/加鉴权，而我们的时间轴、词级字幕、门禁全部建立在这些产物上。
# 这里只做**接口**（不实现第二个后端）：把"合成一句话 -> 音频 + 词表"这个动作抽象出来，
# 将来要换 Azure 官方 TTS / 离线引擎，只加一个 register 的实现，timeline/layout/QA 一行不改。
#
# 协议（实现者必须满足）：
#   fn(text, out_mp3, *, voice, rate, pitch, volume, trim, trim_db, words) -> dict
#   返回的 dict 至少含：path（可播放音频路径）、dur（实测秒数，**必须是时间轴权威**）、
#                       words（可空列表）、trimmed（bool）
TTS_BACKENDS = {}


def register_tts_backend(name, fn, *, note=""):
    """注册一个 TTS 后端实现。name 用于 film.json 的 tts.backend 选择。"""
    if not callable(fn):
        raise TypeError("register_tts_backend 需要一个可调用对象，收到 %r" % (fn,))
    TTS_BACKENDS[str(name)] = dict(fn=fn, note=str(note or ""))
    return str(name)


def tts_backend_names():
    return ["edge"] + sorted(n for n in TTS_BACKENDS if n != "edge")


def tts_words_key(text, voice, rate, pitch, volume="+0%", trim=True,
                   trim_db=-42.0) -> str:
    """词级元数据的独立缓存键。

    为什么不并进 tts_key_v2：并进去会让**所有已有项目的音频缓存一次性失效**（必须重新联网
    合成整片）。分开记 -> 已有音频照旧复用，只有"这次显式要词级"时才补一次事件采集。
    """
    payload = "%s|%s|%s|%s|%s|%s|%s" % ("words", text, voice, rate, pitch,
                                       volume, int(bool(trim)))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def trim_silence_bounds(pcm, sr=SR, thresh_db=-42.0, keep_ms=80):
    """返回 trim_silence 会保留的样本区间 [a, b)。

    词级时间戳需要它：edge-tts 的 WordBoundary offset 是相对**原始 mp3** 的，
    而 trim=True 时 timeline 用的是裁剪后的语音。不减去 a，词就会整体提前 ~0.1–1 s。
    与 trim_silence 共用同一套判据（idx[0]/idx[-1] ± keep），保证两边永远一致。
    """
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.size == 0:
        return 0, 0
    th = 10 ** (thresh_db / 20.0)
    idx = np.where(np.abs(pcm) > th)[0]
    if idx.size == 0:
        return 0, 0
    keep = int(sr * keep_ms / 1000.0)
    a = max(0, int(idx[0]) - keep)
    b = min(len(pcm), int(idx[-1]) + keep)
    return a, b


def words_normalize(events, *, offset_shift=0.0, dur=None, min_gap=0.0):
    """把 edge-tts 的 WordBoundary 事件规整成 "句内词表"。

    输入事件：{"type":"WordBoundary","offset":<100ns>,"duration":<100ns>,"text":str}
    输出行： {"text":str,"start":秒,"dur":秒,"end":秒}（start/end 都是相对本句语音起点，
             offset_shift 为裁剪掉的句首静音秒数，会从 start 里减掉）。

    两个必须处理的现实（实测 edge-tts zh-CN）：
      * 词之间有 0.1–0.3 s 空隙，事件不覆盖空隙 —— 所以 start/dur 会留洞，这是对的；
      * 句尾静音（约 0.5 s）不在任何事件里 —— 排轴时用"整句实测时长"兜底，不用词时长求和。
    """
    out = []
    for ev in events or []:
        txt = str(ev.get("text") or "").strip()
        if not txt:
            continue
        try:
            st = float(ev.get("offset") or 0.0) / 1e7 - float(offset_shift)
            du = float(ev.get("duration") or 0.0) / 1e7
        except (TypeError, ValueError):
            continue
        if st < 0:
            # 裁剪把词首切掉一点：钳到 0，不丢词（丢词会让"字幕少一句"这类门禁误报）
            du = max(0.0, du + st)
            st = 0.0
        row = dict(text=txt, start=round(st, 4), dur=round(du, 4),
                   end=round(st + du, 4))
        out.append(row)
    out.sort(key=lambda r: r["start"])
    # 单调性兜底：极端情况下服务端事件的 offset 可能轻微乱序/重叠，
    # 这里按"后一个不得早于前一个"夹紧，保证下游（字幕、cue 锚点）拿到的是可用的序列。
    prev_end = 0.0
    for r in out:
        if r["start"] < prev_end:
            r["start"] = round(prev_end, 4)
            r["end"] = round(r["start"] + r["dur"], 4)
        prev_end = r["end"]
    if dur is not None and out:
        # 末词终点不得超过整句语音（防止边界事件把尾巴算到下一句）
        lim = float(dur)
        for r in out:
            if r["end"] > lim + 1e-6:
                r["end"] = round(lim, 4)
                r["dur"] = round(max(0.0, r["end"] - r["start"]), 4)
    return out


def words_summary(words, dur=None):
    """词表的自检摘要（给 QC 门禁用：单调性 / 覆盖率 / 末词偏差）。"""
    ws = list(words or [])
    n = len(ws)
    gaps = []
    for i in range(1, n):
        gaps.append(round(float(ws[i]["start"]) - float(ws[i - 1]["end"]), 4))
    covered = round(sum(float(r["dur"]) for r in ws), 4)
    tail = round(float(dur) - float(ws[-1]["end"]), 4) if (dur and n) else None
    mono = all(float(ws[i]["start"]) >= float(ws[i - 1]["start"]) for i in range(1, n))
    return dict(n=n, covered=covered, gaps=gaps, tail=tail, mono=bool(mono),
                start=round(float(ws[0]["start"]), 4) if n else None)


def tts(text, out_mp3, voice="zh-CN-YunyangNeural", rate="+0%", pitch="+0Hz",
        cache=True, retries=4, trim=True, trim_db=-42.0, volume="+0%",
        persona=None, tone=None, words=False, backend="edge"):
    """逐句合成旁白（带输入校验的缓存 + 网络重试 + 可选尾静音裁剪）。

    缓存必须校验输入：只判断文件是否存在，改一句台词就会静默复用旧音频；
    所以把 text/voice/rate/pitch/volume 的哈希写进 meta，变了就重新合成。
    volume 走 edge-tts 原生参数；persona/tone 只作审计字段。
    缓存键升 v2（把 volume/trim 也纳入，修掉"关了裁剪其实没关"的静默 bug）。
    trim=True 时另外落一份裁剪后的 wav 并在 meta.path 指向它，dur 为裁剪后实测时长。
    """
    out_mp3 = str(out_mp3)
    d = os.path.dirname(os.path.abspath(out_mp3))
    if d:
        os.makedirs(d, exist_ok=True)
    # 可替换后端：接口只规定"返回音频 + 词表 + 实测时长"，
    # 默认仍是 edge 的内联实现（逐字节不变）。
    if str(backend or "edge") != "edge":
        impl = (TTS_BACKENDS.get(str(backend)) or {}).get("fn")
        if impl is None:
            raise RuntimeError("未知的 TTS 后端 %r（已注册：%s）"
                               % (backend, ", ".join(tts_backend_names())))
        row = impl(text, out_mp3, voice=voice, rate=rate, pitch=pitch,
                   volume=str(volume or "+0%"), trim=trim, trim_db=trim_db, words=words)
        row = dict(row or {})
        if "dur" not in row or not row.get("path"):
            raise RuntimeError("TTS 后端 %r 的返回值缺 path/dur（协议见 filmkit.TTS_BACKENDS）"
                               % backend)
        row.setdefault("text", text)
        row.setdefault("voice", voice)
        row.setdefault("backend", str(backend))
        return row
    meta_path = out_mp3 + ".json"
    vol = str(volume or "+0%")
    key = tts_key_v2(text, voice, rate, pitch, volume=vol, trim=trim, trim_db=trim_db)
    legacy_key = tts_key(text, voice, rate, pitch)

    wk = tts_words_key(text, voice, rate, pitch, volume=vol, trim=trim,
                       trim_db=trim_db)

    def _words_ok(m):
        """要词级时必须真有词级记录（老缓存没有 -> 补采一次，别静默返回空词表）。"""
        if not words:
            return True
        return bool(m.get("words_key") == wk and m.get("words"))

    audio_cached = None                 # 可用音频（可能缺词级记录）
    if cache and os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 2000:
        try:
            m = json.load(open(meta_path, encoding="utf-8"))
            # v2 命中：key 已经含 volume/trim，不需要再分叉判断 trimmed
            if m.get("key") == key and m.get("text") == text:
                if m.get("trimmed") and m.get("path") and os.path.exists(m["path"]):
                    if _words_ok(m):
                        return m
                    audio_cached = m          # 有音频、缺词级：断网时降级复用它
                if not m.get("trimmed") and not trim:
                    if _words_ok(m):
                        return m
                    audio_cached = m
            # v1 老缓存（v1 键合成的）只有在参数完全等于 v1 语义时才复用，
            # 避免"改了一个 volume 却拿到老音频"这种静默错配。
            # trim 也必须一致：v1 的 key 里没有 trim，旧记录若 trimmed=True，用户把
            # trim_silence 改成 false 后仍会命中并返回**裁剪版**音频（与 v2 键要防的是
            # 同一类"关了裁剪其实没关"，只是漏在 v1 兼容分支上）。
            elif (str(m.get("key") or "") == legacy_key and m.get("text") == text
                  and vol in ("+0%", "") and m.get("voice") == voice
                  and bool(m.get("trimmed")) == bool(trim)):
                if m.get("trimmed") and m.get("path") and os.path.exists(m["path"]):
                    return m
        except Exception:
            pass

    import asyncio
    import time as _time

    raw_events = []
    for attempt in range(max(1, int(retries))):
        try:
            import edge_tts

            async def go():
                # 要词级时不能再用 save()——save 会把 WordBoundary 丢掉（只写音频）。
                # 显式传 boundary="WordBoundary"：默认是 SentenceBoundary，此时一条词事件都收不到
                # （实测 edge-tts：不传 boundary 时 zh-CN 词事件数 = 0）。
                c = edge_tts.Communicate(
                    text, voice, rate=rate, pitch=pitch, volume=vol,
                    boundary="WordBoundary" if words else "SentenceBoundary")
                if not words:
                    await c.save(out_mp3)
                    return
                raw_events.clear()
                # 先写 .part 再 os.replace：中途失败/断网时**绝不能**截断已有的缓存音频
                # （直接 "wb" 打开目标文件，一次失败就把好音频清成 0 字节）。
                part = out_mp3 + ".part"
                try:
                    with open(part, "wb") as fh:
                        async for chunk in c.stream():
                            if chunk["type"] == "audio":
                                fh.write(chunk["data"])
                            else:
                                raw_events.append(dict(chunk))
                    if not os.path.exists(part) or os.path.getsize(part) <= 2000:
                        raise RuntimeError("合成产物异常")
                    os.replace(part, out_mp3)
                finally:
                    if os.path.exists(part):
                        try:
                            os.remove(part)
                        except OSError:
                            pass

            asyncio.run(go())
            if not os.path.exists(out_mp3) or os.path.getsize(out_mp3) <= 2000:
                raise RuntimeError("合成产物异常")
            break
        except Exception as e:              # noqa: BLE001
            if attempt == max(1, int(retries)) - 1:
                if audio_cached is not None and words:
                    # 断网 / 服务端拒绝时的降级：**音频还在**，只是这次没词级。
                    # 不静默：打 WARN，并在 meta 里记 words_ok=False，让 gates 判 skipped。
                    print("   [WARN] %s: 拿不到词级时间戳（%s），复用已有音频；"
                          "本次词级门禁会记 skipped" % (os.path.basename(out_mp3), e),
                          flush=True)
                    audio_cached["words"] = []
                    audio_cached["words_ok"] = False
                    audio_cached["words_events"] = 0
                    return audio_cached
                raise RuntimeError("edge-tts 合成失败（重试 %d 次）：%s" % (retries, e)) from e
            _time.sleep(1.2 * (attempt + 1))

    # trim=True 时同一句 mp3 只需要解码一次：audio_dur() 内部就是 decode_pcm，
    # 不能先 audio_dur 再 decode_pcm：那是每句多起一个 ffmpeg 进程、多做一次全量解码
    # （55 句片就是 55 次多余的解码 + 进程启动，而 TTS 阶段本身是网络瓶颈）。
    pcm_raw = decode_pcm(out_mp3) if trim else None
    dur_raw = len(pcm_raw) / float(SR) if pcm_raw is not None else audio_dur(out_mp3)
    used = out_mp3
    dur = dur_raw
    shift = 0.0                         # 句首被裁掉的静音秒数（词时间戳要减掉它）
    if trim:
        a, b = trim_silence_bounds(pcm_raw, thresh_db=trim_db)
        pcm = pcm_raw[a:b] if b > a else pcm_raw
        wav_path = os.path.splitext(out_mp3)[0] + ".trim.wav"
        wav_write(wav_path, pcm)
        used = wav_path
        dur = len(pcm) / float(SR)
        shift = a / float(SR) if pcm_raw.size else 0.0

    m = dict(path=used, mp3=out_mp3, text=text, voice=voice, rate=rate, pitch=pitch,
             volume=vol, key=key, spec_version=2, dur=dur, dur_raw=dur_raw,
             trimmed=bool(trim), trim_db=float(trim_db))
    if words:
        wlist = words_normalize(raw_events, offset_shift=shift, dur=dur)
        m["words"] = wlist
        m["words_key"] = wk
        m["words_events"] = len(raw_events)
        m["words_shift"] = round(float(shift), 4)
        # 词事件收不到不是崩溃，而是"服务端行为变了"：留空表 + 事件计数，
        # 由 gates 判 skipped 并写进 QC 报告（不许静默当成"有词级"）。
        m["words_ok"] = bool(wlist)
    if persona:
        m["persona"] = str(persona)
    if tone:
        m["tone"] = str(tone)
    json.dump(m, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False)
    return m


def tts_batch(lines, workdir, voice="zh-CN-YunyangNeural", rate="+0%", pitch="+0Hz",
              workers=4, trim=True, plan=None, trim_db=-42.0, words=True,
              backend="edge"):
    """并发逐句合成（并发只影响墙钟，不影响产物：每句独立缓存）。

    plan=[{voice,rate,pitch,volume,persona,tone}, ...]（与 lines 等长）时
    逐句用各自的声线/语气；plan=None 时用上面的全局参数，逐句一致。
    """
    from concurrent.futures import ThreadPoolExecutor

    tts_dir = os.path.join(workdir, "tts")
    os.makedirs(tts_dir, exist_ok=True)
    results = [None] * len(lines)
    specs = list(plan) if plan is not None else [None] * len(lines)
    if len(specs) != len(lines):
        raise ValueError("plan 长度 %d 与 lines 长度 %d 不一致" % (len(specs), len(lines)))

    def one(i, ln):
        spec = specs[i] or {}
        mp3 = os.path.join(tts_dir, str(ln["id"]) + ".mp3")
        m = tts(ln["text"], mp3,
                voice=spec.get("voice") or voice,
                rate=spec.get("rate") or rate,
                pitch=spec.get("pitch") or pitch,
                volume=spec.get("volume") or "+0%",
                trim=trim, trim_db=trim_db, words=words, backend=backend,
                persona=spec.get("persona"), tone=spec.get("tone"))
        row = dict(ln)
        row.update(m)
        print("  tts %-10s %6.2fs  %s" % (ln["id"], m["dur"], ln["text"][:34]), flush=True)
        return i, row

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = [ex.submit(one, i, ln) for i, ln in enumerate(lines)]
        for fu in futs:
            i, row = fu.result()
            results[i] = row
    return results


# ---------------------------------------------------------------- 画幅规划
VIDEO_FPS_ALLOWED = (24, 25, 30, 50, 60)
# 允许的**输出**尺寸，按当前画布的宽高比归档。
# 白名单而不是"任意宽高"：每个尺寸都得确认镜头/字号/安全区在目标分辨率下仍然成立，
# 加新尺寸要重跑一次金帧与 preflight，不是改个数字。
VIDEO_OUT_ALLOWED = ((1920, 1080), (1280, 720), (960, 540), (2560, 1440))
VIDEO_OUT_ALLOWED_PORTRAIT = ((1080, 1920), (720, 1280), (540, 960))
VIDEO_ASPECT = 16.0 / 9.0
VIDEO_ASPECT_PORTRAIT = 9.0 / 16.0


def video_plan(script=None):
    """解析 film.json 的 video 块 -> 内部分辨率 / 输出分辨率 / 帧率。

    设计取舍（都是刻意的）：
      * **输出宽高比必须与当前画布一致**。画布由 `layoutkit` 的 profile 决定（横屏 16:9 /
        竖屏 9:16，见 `--layout`）；输出换成另一个比例就是换了整套版式——安全区、字幕带、
        版式几何全部不成立，所以直接报错，而不是悄悄出个变形或带黑边的片子。
      * **构图恒定 1920x1080，只允许下采样输出**（720p/540p/1440p 里 1440p 需上采样，
        因此默认拒绝）：上采样不会增加信息，只会把"1080p 的清晰度"包装成"4K"，
        与项目"文档真话化 / 不糊弄"的原则冲突。要 4K 就改内部构图分辨率。
      * fps 只接受常见交付值；改 fps 只改时间→帧的换算，不改变画面内容。
    """
    v = dict((script or {}).get("video") or {})
    try:
        fps = int(v.get("fps") or (script or {}).get("fps") or FPS)
    except (TypeError, ValueError):
        raise ValueError("video.fps 必须是整数，收到 %r" % (v.get("fps"),)) from None
    if fps not in VIDEO_FPS_ALLOWED:
        raise ValueError("video.fps=%d 不支持（可用：%s）"
                         % (fps, "/".join(str(x) for x in VIDEO_FPS_ALLOWED)))
    try:
        ow = int(v.get("width") or v.get("out_width") or W)
        oh = int(v.get("height") or v.get("out_height") or H)
    except (TypeError, ValueError):
        raise ValueError("video.width/height 必须是整数，收到 %r/%r"
                         % (v.get("width"), v.get("height"))) from None
    if (ow, oh) == (W, H):
        return dict(fps=fps, w=W, h=H, out_w=ow, out_h=oh, scale=False,
                    label="%dx%d" % (ow, oh))
    # 宽高比必须与**当前画布**一致：竖屏画布下输出横屏（或反过来）等于换了一套版式，
    # 安全区与字幕带全都不成立。这里按画布判断，而不是硬编码 16:9。
    canvas_portrait = H > W
    want_aspect = VIDEO_ASPECT_PORTRAIT if canvas_portrait else VIDEO_ASPECT
    allowed = VIDEO_OUT_ALLOWED_PORTRAIT if canvas_portrait else VIDEO_OUT_ALLOWED
    shape = "竖屏 9:16" if canvas_portrait else "横屏 16:9"
    if abs(ow / float(oh) - want_aspect) > 0.01:
        raise ValueError("video.width=%d height=%d 与当前画布 %dx%d（%s）宽高比不同"
                         "（%.3f:1）：换宽高比要重做版式几何与字幕安全区；"
                         "要出竖屏请用 --layout portrait 起渲染"
                         % (ow, oh, W, H, shape, ow / float(oh)))
    if (ow, oh) not in allowed:
        raise ValueError("video 输出尺寸 %dx%d 不在 %s 的允许列表 %s 内（要新尺寸先确认"
                         "镜头/字号在目标分辨率下仍然成立，再把它加进白名单）"
                         % (ow, oh, shape, ", ".join("%dx%d" % s for s in allowed)))
    if ow > W or oh > H:
        raise ValueError("video 输出 %dx%d 大于内部构图 %dx%d：那是上采样，只会把 1080p 的"
                         "清晰度包装成更高分辨率（假 4K）。要真高清请改内部构图分辨率，"
                         "而不是放大输出。" % (ow, oh, W, H))
    return dict(fps=fps, w=W, h=H, out_w=ow, out_h=oh, scale=True,
                label="%dx%d" % (ow, oh))


# ---------------------------------------------------------------- 时间轴
def build_timeline(lines, lead=0.55, tail=0.85, min_dur=3.0, tail_map=None, max_dur=None,
                   hold=0.0, hold_map=None, bar=0.0, lead_map=None, fps=None):
    """由实测语音时长反推时间轴。第一段 start 必须严格为 0。

    hold / hold_map：语音结束后额外停留的秒数——这就是"停留窗口"：
      观众没看清的地方加停留，而不是把整片放慢。hold 为全局默认，hold_map 按 id 覆盖，
      行内 `hold` 字段优先级最高。bar > 0 时把每段时长向上取整到 bar 的整数倍
      （配乐小节对齐用，例如 bar=2.0 = 120 BPM 的一小节）。

    max_dur 只能截留白，不能截到语音结束之前：段时长被压到 lead+语音以下时，场景变短
    而旁白仍按 voice_start 播完整长句 -> 下一句的语音压在本句上（音频总线里两条人声
    重叠），字幕窗口也会倒挂。所以取 max(max_dur, 硬下限) 并打印 WARN；真要缩短请减
    句长或改 lead/tail。
    """
    scenes, t = [], 0.0
    for ln in lines:
        voice = float(ln.get("dur") or 0.0)
        extra = float(ln.get("hold", (hold_map or {}).get(ln["id"], hold)) or 0.0)
        # lead 也支持逐句（行内 lead > lead_map > 全局），
        # 与 tail_map 同构 —— 不同语气档的"句前呼吸"本来就不该一样长。
        lead_i = float(ln.get("lead", (lead_map or {}).get(ln["id"], lead)))
        tail_i = float((tail_map or {}).get(ln["id"], tail))
        d = max(float(min_dur), lead_i + voice + tail_i + extra)
        if max_dur and d > float(max_dur):
            # 硬下限 = min_dur 与 "句前留白 + 语音" 的较大者：min_dur 是"画面撑住"的下限，
            # lead+voice 是"人声不被切断/不与下一句重叠"的物理下限。max_dur 只压得动
            # tail/hold 这些留白；压到语音以下时必须报警（静默重叠比参数没生效更糟）。
            floor = max(float(min_dur), lead_i + voice)
            d = max(float(max_dur), floor)
            if d > float(max_dur) + 1e-9:
                print("   [WARN] build_timeline: %s 的 lead %.2fs + 语音 %.2fs 超过 "
                      "max_dur=%.2fs，段时长取 %.2fs（不切断旁白；要更短请减句长或调 lead）"
                      % (ln.get("id", "?"), lead_i, voice, float(max_dur), d), flush=True)
        if bar and float(bar) > 0:
            d = math.ceil(d / float(bar) - 1e-9) * float(bar)
        row = dict(ln, start=round(t, 3), end=round(t + d, 3), dur=round(d, 3),
                   voice_start=round(t + lead_i, 3),
                   voice_end=round(t + lead_i + voice, 3))
        # 把句内词表平移成**绝对时间**，下游（字幕断行 / cue 词锚点 /
        # 词级门禁）只需要读 words_abs，不必自己再算一遍 offset。
        if ln.get("words"):
            vs = row["voice_start"]
            row["words_abs"] = [dict(w, start=round(vs + float(w["start"]), 4),
                                     end=round(vs + float(w["end"]), 4))
                                for w in ln["words"]]
        scenes.append(row)
        t += d
    if scenes:
        scenes[0]["start"] = 0.0
    f = int(fps or FPS)
    return dict(total=round(t, 3), scenes=scenes, fps=f, frames=int(round(t * f)))


def sub_window(scenes, i, max_hold=None):
    """字幕窗口：默认覆盖整句语音，且绝不压下一句。

    默认不钳：把 t1 钳在 voice_start + 5.2 会让 10 秒长句字幕在读到一半就消失
    （语速 +8% 的长句尤其明显）。需要钳短时显式传 max_hold。
    """
    s = scenes[i]
    t0 = s["voice_start"] - 0.12
    t1 = s["voice_end"] + 0.45
    if max_hold and float(max_hold) > 0:
        # 0/负值当"不限制"：传 -0.5 会把 t1 钳到 voice_start-0.5，窗口倒挂、
        # 整句字幕消失。sub_max_hold 是公共参数，外部调用可能原样透传。
        t1 = min(t1, s["voice_start"] + float(max_hold))
    t1 = min(t1, s["end"] - 0.06)
    nxt = scenes[i + 1]["voice_start"] - 0.12 if i + 1 < len(scenes) else 1e9
    return t0, min(t1, nxt)


def sub_at(scenes, t, max_hold=None):
    for i, s in enumerate(scenes):
        t0, t1 = sub_window(scenes, i, max_hold)
        if t0 <= t <= t1:
            return s.get("sub") or s.get("text", "")
    return ""


def sub_words_at(scenes, t, max_hold=None):
    """取当前时刻上屏字幕对应的**词表**；没有词级就返回 None。"""
    for i, s in enumerate(scenes):
        t0, t1 = sub_window(scenes, i, max_hold)
        if t0 <= t <= t1:
            return s.get("words_abs") or None
    return None


def write_srt(scenes, path, max_hold=None):
    def fmt(x):
        ms = int(round(x * 1000))
        h, ms = ms // 3600000, ms % 3600000
        m, ms = ms // 60000, ms % 60000
        s, ms = ms // 1000, ms % 1000
        return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

    rows = []
    for i, s in enumerate(scenes):
        t0, t1 = sub_window(scenes, i, max_hold)
        # 时间码不能为负、不能倒挂：首句 lead < 0.12 时 t0 = -0.12，直接写会得到
        # "-1:59:59,880" 这种非法行（播放器整条丢弃或直接报错）。
        t0 = max(0.0, t0)
        t1 = max(t0, t1)
        body = s.get("sub") or s.get("text", "")
        rows.append("%d\n%s --> %s\n%s\n" % (i + 1, fmt(t0), fmt(t1), body))
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(rows))
    return len(rows)


def write_ass(scenes, path, max_hold=None, font_name="Microsoft YaHei", size=52,
              theme=None):
    """直接写 ASS，并在头里声明 PlayResX/Y=1920/1080。

    这样交 libass 不会按 PlayResY=288 反推，避免"字号小得看不见"
    （已知坑：SRT 交 libass 需要 FontSize/MarginV 乘 3.75）。
    """
    def fmt(x):
        cs = int(round(x * 100))
        h, cs = cs // 360000, cs % 360000
        m, cs = cs // 6000, cs % 6000
        s, cs = cs // 100, cs % 100
        return "%d:%02d:%02d.%02d" % (h, m, s, cs)

    primary, outline, back = "&H00FCF8F5", "&H00101010", "&H96000000"
    if isinstance(theme, dict):
        # ASS 的颜色是 &HAABBGGRR（BGR 顺序 + alpha 在前，alpha 0=不透明）
        def _ass(rgb, alpha=0):
            r, g, b = rgb
            return "&H%02X%02X%02X%02X" % (int(alpha), int(b), int(g), int(r))
        primary = _ass(theme["ink"])
        outline = _ass(theme["bg2"])
        back = _ass(theme["surface"], 0x60)
        fam = str(theme.get("family") or "sans")
        # 字体族 -> Windows 上的实际字体名（libass 认 name 不认路径）
        font_name = {"serif": "SimSun", "kai": "KaiTi", "round": "DengXian"}.get(fam, "Microsoft YaHei")
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: %d" % W,
        "PlayResY: %d" % H,
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,%s,%d,%s,&H000000FF,%s,%s,-1,0,0,0,100,100,0,0,"
        "1,3,1,2,60,60,56,1" % (font_name, int(size), primary, outline, back),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    body = []
    for i, s in enumerate(scenes):
        t0, t1 = sub_window(scenes, i, max_hold)
        # 同 write_srt：ASS 的 &H 时间码同样不接受负值（-0.12s 会写成 -1:59:59.88）。
        t0 = max(0.0, t0)
        t1 = max(t0, t1)
        txt = (s.get("sub") or s.get("text", "")).replace("\n", "\\N")
        body.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (fmt(t0), fmt(t1), txt))
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(head + body) + "\n")
    return len(body)


# ---------------------------------------------------------------- 帧写入
class FrameSink:
    """PIL 帧 -> rawvideo 管道 -> libx264。

    strict=True（默认）时尺寸不符直接报错：静默 resize 时
    上限内看着没事，下游却拿不到"场景写错了分辨率"的信号。
    """

    def __init__(self, out_path, w=W, h=H, fps=FPS, crf=18, preset="medium",
                 audio=None, bitrate=None, strict=True, extra_args=None,
                 out_w=None, out_h=None, scale_flags="lanczos"):
        self.out = str(out_path)
        self.w, self.h, self.fps = int(w), int(h), float(fps)
        self.strict = strict
        self.frames = 0
        # 内部构图恒为 w x h（PIL 画布），输出可以更小（只允许下采样）。
        # 缩放放在**编码这一遍**做（-vf scale），不二次编码、也不改变合成内容：
        # 1920→1280 的 lanczos 下采样本身等于 1.5x 超采样，画面不会因为降规格而变糊。
        self.out_w = int(out_w or self.w)
        self.out_h = int(out_h or self.h)
        if self.out_w > self.w or self.out_h > self.h:
            raise ValueError("FrameSink: 输出 %dx%d 大于内部构图 %dx%d，拒绝上采样"
                             % (self.out_w, self.out_h, self.w, self.h))
        self.scale = (self.out_w, self.out_h) != (self.w, self.h)
        d = os.path.dirname(os.path.abspath(self.out))
        if d:
            os.makedirs(d, exist_ok=True)
        cmd = [FFMPEG, "-y", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (self.w, self.h),
               "-r", "%g" % self.fps, "-i", "-"]
        if audio:
            cmd += ["-i", str(audio)]
        if self.scale:
            cmd += ["-vf", "scale=%d:%d:flags=%s" % (self.out_w, self.out_h, scale_flags)]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-g", str(int(self.fps * 2)),
                "-keyint_min", str(int(self.fps * 2)), "-sc_threshold", "0"]
        if bitrate:
            cmd += ["-maxrate", bitrate, "-bufsize", bitrate]
        if audio:
            ad = media_dur(audio)
            if ad:
                cmd += ["-t", "%.3f" % ad]      # 显式 -t；不用 -shortest（会按较短一路截断）
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", self.out]
        if extra_args:
            cmd += list(extra_args)
        self.cmd = cmd
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def write(self, frame):
        if frame is None:
            raise RuntimeError("场景返回了 None（帧 %d）" % self.frames)
        if frame.size != (self.w, self.h):
            if self.strict:
                raise RuntimeError("帧尺寸 %dx%d != 期望 %dx%d（场景函数必须返回目标分辨率）"
                                   % (frame.size[0], frame.size[1], self.w, self.h))
            frame = frame.resize((self.w, self.h), Image.LANCZOS)
        if frame.mode != "RGB":
            frame = frame.convert("RGB")
        self.proc.stdin.write(frame.tobytes())
        self.frames += 1

    def close(self):
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            # 编码进程已经死了：真正的死因在 stderr 里，下面统一报，别让 BrokenPipe
            # 把 ffmpeg 的错误信息顶掉（只有裸 close 时异常看不到任何诊断）。
            pass
        # 先排空 stderr 再 wait：-v error 的报错行也可能多到写满 64KB 管道，
        # 而 wait() 期间没人读它 -> ffmpeg 卡在写 stderr、双方一起死等。
        err = self.proc.stderr.read() if self.proc.stderr is not None else b""
        rc = self.proc.wait()
        if rc != 0:
            raise RuntimeError("ffmpeg 编码失败（exitcode=%d）: %s"
                               % (rc, err.decode("utf-8", "replace")[:800]))
        return self.out


# ---------------------------------------------------------------- 合成与封装
def media_dur(path):
    p = _run([ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", str(path)])
    try:
        return float((p.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def probe(path):
    p = _run([ffprobe_path(), "-v", "error",
              "-show_entries", "format=duration,size,bit_rate",
              "-show_entries", "stream=codec_name,codec_type,width,height,r_frame_rate,nb_frames,"
              "sample_rate,channels",
              "-of", "json", str(path)])
    try:
        return json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def mux(video, audio, out, crf=18, preset="medium", audio_bitrate="192k",
        loudnorm=True, target_i=-16.0, target_tp=-1.5, reencode_video=False):
    """合成成片：视频流默认直接 copy（不再二次编码），音频走两遍 loudnorm。

    不能对已经 CRF18 编好的 silent.mp4 再编一遍 libx264：既有二次有损
    又是纯浪费时间。视频里没有需要再次编码的信息，copy 是正确选择；
    需要改分辨率/插字幕时才用 reencode_video=True。
    """
    d = os.path.dirname(os.path.abspath(str(out)))
    if d:
        os.makedirs(d, exist_ok=True)
    af = loudnorm_two_pass(audio, target_i=target_i, target_tp=target_tp) if loudnorm else None
    if loudnorm and not af:
        # 不静默降级：量不到就喊出来（qa 的响度门禁还在，但这里先说清楚）
        print("   [WARN] loudnorm 两遍测量失败（拿不到可用测量值：全静音/极短音频会给出 -inf），"
              "本次不做响度标准化", flush=True)

    vd = media_dur(video)
    ad = media_dur(audio)
    if vd and ad and abs(vd - ad) > 0.05:
        raise RuntimeError("音视频时长不一致：video %.3fs vs audio %.3fs（差 %.3fs）。"
                           "检查混音总线长度是否等于 timeline.total。"
                           % (vd, ad, abs(vd - ad)))
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(video), "-i", str(audio)]
    if reencode_video:
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    if af:
        cmd += ["-af", af]
    cmd += ["-c:a", "aac", "-b:a", audio_bitrate, "-ar", str(SR)]
    if vd:
        cmd += ["-t", "%.3f" % vd]
    cmd += ["-movflags", "+faststart", str(out)]
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError("mux 失败: " + p.stderr.decode("utf-8", "replace")[:800])
    return str(out)


def loudnorm_two_pass(src, target_i=-16.0, target_tp=-1.5, target_lra=11.0):
    """两遍 loudnorm：先测量，再按测量值归一化。返回可直接给 -af 的滤镜串。"""
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(src), "-af",
              "loudnorm=I=%.1f:TP=%.1f:LRA=%.1f:print_format=json"
              % (target_i, target_tp, target_lra), "-f", "null", "-"])
    txt = p.stderr.decode("utf-8", "replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", txt, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except Exception:
        return None
    # 全静音 / 极短音频时 ffmpeg 会报 input_i=-inf、target_offset=inf：这些值塞进
    # measured_* 会被 ffmpeg 拒绝（"Value -inf for parameter 'measured_I' out of range"），
    # mux 直接失败（只判"解析到 JSON"就会拿着 -inf 的滤镜串去编码）。
    # 量不到可用测量值就返回 None：mux 会打 WARN 跳过响度标准化，qa 的响度门禁照旧兜底。
    need = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    try:
        bad = [k for k in need if not math.isfinite(float(j.get(k, 0.0)))]
    except (TypeError, ValueError):
        return None
    if bad:
        return None
    return ("loudnorm=I=%.1f:TP=%.1f:LRA=%.1f:measured_I=%s:measured_TP=%s:"
            "measured_LRA=%s:measured_thresh=%s:offset=%s:print_format=summary"
            % (target_i, target_tp, target_lra, j.get("input_i"), j.get("input_tp"),
               j.get("input_lra"), j.get("input_thresh"), j.get("target_offset", 0.0)))


def loudness(path):
    """EBU R128 积分响度 + 真峰。必须显式开 ebur128=peak=true：默认参数拿不到 Peak，真峰门禁永远不触发。"""
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
              "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"])
    txt = p.stderr.decode("utf-8", "replace")
    out = {}
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", txt)
    if m:
        out["I"] = float(m[-1])
    m = re.findall(r"LRA:\s*(-?[\d.]+)\s*LU", txt)
    if m:
        out["LRA"] = float(m[-1])
    m = re.findall(r"Peak:\s*(-?[\d.]+)\s*dBFS", txt)
    if m:
        out["TP"] = float(m[-1])
    return out


def detect_black(path, dur=0.4, head=1.2, tail=None, pix_th=None):
    """长黑帧检测。head/tail 秒内是刻意的淡入淡出，不算问题。

    返回 None 表示**检测根本没跑成**（ffmpeg 打不开文件 / blackdetect 失败）。
    这一点必须可区分：失败也返回 0 时，qa 门禁会把"没查"记成"0 段黑帧 = 通过"。

    pix_th：多"暗"算黑（0..1）。**必须跟着主题底色走**，否则暗色主题会被整片误判。
    blackdetect 的默认 pix_th 是 0.10，意思是"亮度低于 10% 的像素算黑"；
    而本项目的近黑金主题底色是 (8,8,10) ≈ 0.031 —— 全片 98% 的像素都在"黑"的阈值以下，
    于是 blackdetect 把整段正常画面报成"4 段长黑帧"。实测：真实亮度全程 0.05–0.07，
    一段真正的黑屏都没有，但门禁报了 4 段。
    """
    if pix_th is None:
        # 缺省仍取 ffmpeg 自带值，保证老调用方行为不变
        pix_th = 0.10
    vf = "blackdetect=d=%s:pic_th=0.98:pix_th=%.4f" % (dur, float(pix_th))
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vf",
              vf, "-f", "null", "-"])
    if p.returncode != 0:
        return None
    total = media_dur(path)
    tl = tail if tail is not None else head
    bad = 0
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)",
                         p.stderr.decode("utf-8", "replace")):
        s, e = float(m.group(1)), float(m.group(2))
        if s >= head and (total <= 0 or e <= total - tl):
            bad += 1
    return bad


def detect_silence(path, th=-45, dur=1.2):
    """长静音段计数。返回 None = 检测没跑成（理由同 detect_black，不许当 0 计入通过）。"""
    p = _run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af",
              "silencedetect=n=%ddB:d=%s" % (th, dur), "-f", "null", "-"])
    if p.returncode != 0:
        return None
    return len(re.findall("silence_start", p.stderr.decode("utf-8", "replace")))


def qa_text_safe(scenes, tol=1.0, band=SUB_BAND):
    """字幕几何校验：宽度/行数/是否触及画面底部。"""
    errs = []
    for s in scenes:
        body = s.get("sub") or s.get("text") or ""
        if not body:
            continue
        lines, f = fit_sub_lines(body, max_w=band[2] - band[0])
        widest = max(text_w(l, f) for l in lines)
        total_h = int(f.size * 1.32) * len(lines)
        if len(lines) > 2:
            errs.append("字幕超过 2 行（%d 行）: %s" % (len(lines), body[:24]))
        if widest > band[2] - band[0]:
            errs.append("字幕过宽 %dpx: %s" % (widest, body[:24]))
        if band[3] - total_h < band[1]:
            errs.append("字幕过高 %dpx: %s" % (total_h, body[:24]))
        if band[3] > H - 12:
            errs.append("字幕带触及画面底部，有被裁风险")
    return errs


# ---------------------------------------------------------------- 编码质量
# cv2 在 README / THIRD_PARTY / requirements.txt 里都写成**可选**，所以这里必须真的可选：
# 函数内裸 `import cv2` 时，装了才算"可选"——没装时 --stage qa 直接 ImportError 崩掉，
# 而文档写"缺了跳过锐度门禁"。所以缺 cv2 时返回 None，由调用方打 WARN 并跳过该门禁。
def _cv2():
    """按需取 cv2；没装返回 None（调用方负责降级 + 报警，不静默）。"""
    try:
        import cv2
    except Exception:                                       # noqa: BLE001
        return None
    return cv2


def sharpness(png_or_img):
    """文字/线条锐度：Laplacian 方差。没装 cv2 时返回 None（不是 0.0，别把"没测"当"很差"）。"""
    cv2 = _cv2()
    if cv2 is None:
        return None
    if isinstance(png_or_img, str):
        im = cv2.imread(str(png_or_img), cv2.IMREAD_GRAYSCALE)
    else:
        im = np.array(png_or_img.convert("L"))
    if im is None:
        return 0.0
    return float(cv2.Laplacian(im, cv2.CV_64F).var())


def snap(path, seconds, out_png):
    d = os.path.dirname(os.path.abspath(out_png))
    if d:
        os.makedirs(d, exist_ok=True)
    _run([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % float(seconds), "-i", str(path),
          "-frames:v", "1", str(out_png)])
    return out_png


def encode_quality(film, ref_png, at_sec=None, ref_img=None):
    """同帧"原始渲染"vs"解码帧"的锐度比。

    ⚠ 关键：ref 必须是场景函数渲染出的原图，不能用成片自己抽的帧，
    否则 sharp_ref 与 sharp_dec 几乎相同、ratio 恒为 1，门禁形同虚设
    （成片 0.922 这样的真实值，需要正确参考帧才测得出来）。

    没装 cv2 时返回 None（调用方据此跳过锐度门禁并 WARN）。
    """
    cv2 = _cv2()
    if cv2 is None:
        return None
    if ref_img is not None:
        ref = np.array(ref_img.convert("L"))
    else:
        ref = cv2.imread(str(ref_png), cv2.IMREAD_GRAYSCALE)
    if ref is None:
        return None
    # 解码帧写进系统临时目录：写在 cwd / 参考帧目录时，跑一次 --stage qa
    # 就在项目根留一个 250KB 的 _dec_tmp.png（既污染目录，也可能被当成交付物）。
    with tempfile.TemporaryDirectory(prefix="filmkit_qa_") as td:
        tmp = os.path.join(td, "_dec_tmp.png")
        snap(film, at_sec if at_sec is not None else 0.0, tmp)
        dec = cv2.imread(tmp, cv2.IMREAD_GRAYSCALE)
    if dec is None:
        return None
    if ref.shape != dec.shape:
        # 输出降到 720p 时，参考帧还是 1080p 的原图。不缩放就比 Laplacian 方差，
        # 等于拿两个不同分辨率的东西比"锐不锐"——ratio 会假性偏低（只在
        # 1920x1080 单一时才碰巧同尺寸）。这里统一缩到**交付分辨率**再比。
        ref = cv2.resize(ref, (dec.shape[1], dec.shape[0]), interpolation=cv2.INTER_AREA)
    sa = float(cv2.Laplacian(ref, cv2.CV_64F).var())
    sb = float(cv2.Laplacian(dec, cv2.CV_64F).var())
    return dict(sharp_ref=round(sa, 1), sharp_dec=round(sb, 1),
                ratio=round(sb / sa, 4) if sa > 0 else 0.0)


def contact_sheet(frames, out_png, cols=4, thumb_w=480, labels=None):
    if not frames:
        return None
    ims = [Image.open(f) if isinstance(f, str) else f for f in frames]
    tw = int(thumb_w)
    th = int(tw * H / W)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (12, 14, 18))
    for i, im in enumerate(ims):
        sheet.paste(im.resize((tw, th), Image.LANCZOS), ((i % cols) * tw, (i // cols) * th))
    d = os.path.dirname(os.path.abspath(out_png))
    if d:
        os.makedirs(d, exist_ok=True)
    sheet.save(out_png)
    return out_png


# ---------------------------------------------------------------- 拼接
# 混 fps 的片段直接 -c copy 会拼出半速成片还不报错。
# 所以拼前先查参数一致性；不一致或 copy 失败都自动降级重编码，只有重编码也失败才 raise。
_LAST_CONCAT = {}


def concat_last_state():
    """最近一次 concat_segments 的结果（mode/reencoded/issues/out/concat_txt）。

    返回副本；没跑过返回 {}。给测试和 backend 回显判断"这次到底有没有重编码"用，
    不改变 concat_segments 的返回值（仍然是 str(out)）。
    """
    return dict(_LAST_CONCAT)


def _set_concat_state(**kw):
    _LAST_CONCAT.clear()
    _LAST_CONCAT.update(kw)


def _stderr_brief(raw, limit=300):
    """ffmpeg stderr 摘要：真正的错误通常在一堆行里的最后几条，取末尾三行拼成一句。"""
    txt = (raw or b"").decode("utf-8", "replace").strip()
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return "（ffmpeg 没有输出错误信息）"
    return " | ".join(lines[-3:])[:limit]


def _concat_quote(path):
    """生成 ffmpeg concat 清单的一行：绝对路径 + 单引号转义。

    concat demuxer 的引号规则照抄 shell：单引号内不能再出现单引号，
    标准写法是把 ' 变成 '\''（先闭合、转义一个引号、再打开）。
    路径里真的含单引号（Windows 文件名允许）也不会坏。
    """
    p = os.path.abspath(str(path)).replace("\\", "/")
    return "file '%s'\n" % p.replace("'", "'\\''")


def _probe_concat_part(path):
    """单段探测（纯读）：返回 dict，ok=False 时 why 说明原因。"""
    p = str(path)
    if not os.path.isfile(p):
        return dict(ok=False, path=p, why="文件不存在")
    pr = probe(p)
    streams = pr.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        return dict(ok=False, path=p, why="ffprobe 读不出视频流（文件损坏或不是视频）")
    rate = str(v.get("r_frame_rate") or "0/0")
    fps = 0.0
    try:
        num, _, den = rate.partition("/")
        fps = float(num) / float(den or 1)
    except Exception:                       # noqa: BLE001 —— 脏帧率不崩，交给诊断
        fps = 0.0
    dur = 0.0
    try:
        dur = float((pr.get("format") or {}).get("duration") or 0)
    except Exception:                       # noqa: BLE001
        dur = 0.0
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    # 音频参数也要探测：-c copy 拼接时各段采样率/声道不一致不会被 ffmpeg 校正，
    # 而是按参照段解释（实测 48k 段 + 44.1k 段拼完频率 440Hz -> 478.6Hz，
    # 即整段变调 1.086 倍，而 ffmpeg 返回码仍是 0）。
    return dict(ok=True, path=p, width=int(v.get("width") or 0),
                height=int(v.get("height") or 0), fps=round(fps, 3),
                fps_text=rate, codec=str(v.get("codec_name") or ""),
                audio=bool(a), dur=dur,
                ar=int(a.get("sample_rate") or 0) if a else 0,
                channels=int(a.get("channels") or 0) if a else 0)


def _fmt_fps(info):
    """帧率说人话：30/1 -> 30；30000/1001 -> 29.97；读不出来 -> 原始文本。"""
    try:
        fps = float(info.get("fps") or 0.0)
    except Exception:                       # noqa: BLE001
        fps = 0.0
    if fps <= 0:
        return str(info.get("fps_text") or "未知")
    if abs(fps - round(fps)) < 0.01:
        return "%d" % int(round(fps))
    return "%.2f" % fps


def _concat_issues(parts):
    """逐段探测并与参照段比对（纯读）。返回 (infos, issues, ref_index)。

    参照段 = 第一个能读出来的段（正常就是第 1 段）；每条 issue 都写清"哪段、差在哪"。
    """
    infos = [_probe_concat_part(p) for p in parts]
    issues = []
    ref = None
    for i, inf in enumerate(infos):
        if not inf.get("ok"):
            issues.append("第 %d 段%s：%s" % (i + 1, inf.get("why", "探测失败"),
                                             inf.get("path", parts[i])))
            continue
        if ref is None:
            ref = i
            continue
        r = infos[ref]
        who, base = "第 %d 段" % (i + 1), "第 %d 段" % (ref + 1)
        if (inf["width"], inf["height"]) != (r["width"], r["height"]):
            issues.append("%s分辨率 %dx%d != %s %dx%d"
                          % (who, inf["width"], inf["height"], base, r["width"], r["height"]))
        if abs(float(inf["fps"]) - float(r["fps"])) > 0.01:
            issues.append("%s fps %s != %s fps %s"
                          % (who, _fmt_fps(inf), base, _fmt_fps(r)))
        if inf["codec"] != r["codec"]:
            issues.append("%s视频编码 %s != %s %s"
                          % (who, inf["codec"] or "?", base, r["codec"] or "?"))
        if bool(inf["audio"]) != bool(r["audio"]):
            issues.append("%s%s音轨，%s%s音轨"
                          % (who, "有" if inf["audio"] else "无",
                             base, "有" if r["audio"] else "无"))
        elif inf["audio"] and r["audio"]:
            # 有音轨时再比音频参数：copy 不做重采样/重混，参数不一致 ->
            # 音高/时长/声道被按参照段解释（错片但返回码 0，属于"静默错"，必须走重编码）。
            if inf.get("ar") and r.get("ar") and inf["ar"] != r["ar"]:
                issues.append("%s音频采样率 %d Hz != %s %d Hz（-c copy 会让音高/时长错位）"
                              % (who, inf["ar"], base, r["ar"]))
            if inf.get("channels") and r.get("channels") and inf["channels"] != r["channels"]:
                issues.append("%s音频声道数 %d != %s %d"
                              % (who, inf["channels"], base, r["channels"]))
    return infos, issues, ref


def concat_preflight(parts):
    """拼接前的参数一致性检查（纯读：不写文件、不改全局状态、不碰 out）。

    逐段 ffprobe 比对 width / height / r_frame_rate / codec_name（外加有无音轨），
    以第一个能读出来的段为参照。返回 (ok, issues)：
      ok=False 表示不能直接 -c copy，调用方应降级重编码；issues 是可直接打印的中文诊断。
    注意：这里只回答"能不能 copy"，不检查画面内容 / 时间轴是否正确。
    """
    parts = [str(p) for p in parts]
    if not parts:
        return False, ["parts 为空（没有可拼接的片段）"]
    _infos, issues, _ref = _concat_issues(parts)
    return (not issues), issues


def _concat_reencode(parts, out, infos, ref, lst):
    """降级路径：重编码后再拼（libx264 crf20 + aac 192k），返回 str(out)。

    不用"concat demuxer + 重编码"：那仍然要求各段分辨率/像素格式一致，混分辨率时
    会花屏或直接失败。这里用 concat 滤镜，逐段把画面统一到参照段的宽高/帧率
    （等比缩放 + 补边，不拉伸），音轨统一 48kHz 立体声；某段没音轨就补等长静音，
    避免 v=1:a=1 的 concat 滤镜拒收。只有所有段都没音轨时才输出纯视频。
    """
    if ref is None:
        raise RuntimeError(
            "[concat] 降级失败：%d 段全都读不出视频流。先逐段跑 ffprobe -show_streams "
            "确认文件没坏；concat.txt 保留在：%s" % (len(parts), lst))
    r = infos[ref]
    tw, th = int(r.get("width") or 0), int(r.get("height") or 0)
    if tw <= 0 or th <= 0:
        tw, th = W, H
    # yuv420p 要求偶数宽高（参照段是奇数尺寸时裁掉 1px，别让重编码挂在最后一步）
    tw, th = max(2, tw - tw % 2), max(2, th - th % 2)
    fps_arg = str(r.get("fps_text") or "")
    if fps_arg in ("", "0/0"):
        fps_arg = "%g/1" % (float(r.get("fps") or 0) or float(FPS))
    any_audio = any(inf.get("ok") and inf.get("audio") for inf in infos)
    ref_dur = max(float(r.get("dur") or 0), 0.0)
    fc = []
    for i, inf in enumerate(infos):
        fc.append("[%d:v]fps=%s,scale=%d:%d:force_original_aspect_ratio=decrease,"
                  "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v%d]"
                  % (i, fps_arg, tw, th, tw, th, i))
        if not any_audio:
            continue
        if inf.get("ok") and inf.get("audio"):
            fc.append("[%d:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a%d]"
                      % (i, i))
        else:
            # 静音长度必须等于**这一段自己的**视频长度。取 max(段长, 参照段长)：
            # 参照段更长时会给出短的无音轨段多补几秒静音，拼接后音频比视频长
            # （实测 3s 有声 + 1s 无声 -> 输出 6s，视频只有 4s，后半段音画差 2s）。
            # 段长读不出来（ffprobe 没有 duration）时才退回参照段时长。
            seg_dur = max(float(inf.get("dur") or 0), 0.0)
            dur = max(seg_dur if seg_dur > 0 else ref_dur, 0.1)
            fc.append("anullsrc=r=48000:cl=stereo,atrim=duration=%.3f,asetpts=PTS-STARTPTS[a%d]"
                      % (dur, i))
    labels = "".join("[v%d]%s" % (i, "[a%d]" % i if any_audio else "")
                     for i in range(len(parts)))
    fc.append("%sconcat=n=%d:v=1:a=%d[v]%s"
              % (labels, len(parts), 1 if any_audio else 0, "[a]" if any_audio else ""))
    fc_text = ";".join(fc)

    cmd = [FFMPEG, "-y", "-v", "error"]
    for src in parts:
        cmd += ["-i", str(src)]
    script_path = None
    try:
        if len(fc_text) > 8000:
            # 段数多时滤镜串会撞 Windows 命令行长度上限：落成临时脚本再引用
            fd, script_path = tempfile.mkstemp(prefix="filmkit_concat_", suffix=".txt")
            os.close(fd)
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(fc_text)
            cmd += ["-filter_complex_script", script_path]
        else:
            cmd += ["-filter_complex", fc_text]
        cmd += ["-map", "[v]"]
        if any_audio:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        proc = _run(cmd)
    finally:
        if script_path:
            try:
                os.remove(script_path)
            except OSError:
                pass
    if proc.returncode != 0:
        raise RuntimeError(
            "[concat] 重编码拼接也失败：%d 段既不能 -c copy，concat 滤镜重编码也没成。\n"
            "  先逐段跑 ffprobe -show_streams 找坏段/缺流；concat.txt 保留在：%s\n"
            "  ffmpeg: %s" % (len(parts), lst, _stderr_brief(proc.stderr)))
    return str(out)


def concat_segments(parts, out, workdir=None):
    """分段拼接：参数一致走 -c copy（快、无损），不一致或 copy 失败自动降级重编码。

    向后兼容：签名和返回值不变（返回 str(out)）。降级发生时打印 [concat] 诊断，
    并把 mode/issues/reencoded 记到 concat_last_state()，供调用方和测试判断。
    """
    parts = [str(p) for p in parts]
    if not parts:
        raise RuntimeError("concat_segments: parts 为空（没有可拼接的片段）")
    _LAST_CONCAT.clear()                    # 失败时别让上一次的 mode 误导调用方
    lst = os.path.join(workdir or os.path.dirname(str(out)), "concat.txt")
    d = os.path.dirname(os.path.abspath(lst))
    if d:
        os.makedirs(d, exist_ok=True)
    # 必须写绝对路径：concat 文件里的相对路径按 ffmpeg 的 cwd 解析，
    # 而不是按 concat.txt 所在目录（实测会拼成 work2/work2/... 打不开）。
    with open(lst, "w", encoding="utf-8") as f:
        for src in parts:
            f.write(_concat_quote(src))
    infos, issues, ref = _concat_issues(parts)
    base_state = dict(out=str(out), parts=len(parts), issues=list(issues), concat_txt=lst)
    if not issues:
        proc = _run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                     "-i", lst, "-c", "copy", str(out)])
        if proc.returncode == 0:
            _set_concat_state(mode="copy", reencoded=False, **base_state)
            return str(out)
        detail = _stderr_brief(proc.stderr)
        print("[concat] -c copy 失败（%s），降级为重编码拼接（libx264 crf20 + aac 192k）"
              % detail, flush=True)
        base_state = dict(base_state, copy_error=detail)
    else:
        print("[concat] 拼接前检查发现 %d 处参数不一致：" % len(issues), flush=True)
        for it in issues:
            print("   [concat] " + it, flush=True)
        summ = "；".join(issues[:3])
        if len(issues) > 3:
            summ += "；等 %d 处" % len(issues)
        print("[concat] 参数不一致（%s），已降级为重编码（libx264 crf20 + aac 192k）"
              % summ, flush=True)
        print("   [concat] concat.txt 保留供排查：%s" % lst, flush=True)
    _concat_reencode(parts, out, infos, ref, lst)
    _set_concat_state(mode="reencode", reencoded=True, **base_state)
    return str(out)


# ---------------------------------------------------------------- 总质检
def qa_report(path, expect_frames=None, ref_png=None, ref_sec=None, ref_img=None,
              expected_subtitles=None, min_bitrate=0, notes=None, theme=None,
              expect_w=None, expect_h=None, expect_fps=None):
    """分档质检：errors 必须修，warnings 看情况。

    三条判据的来由：
    - 真峰必须能测到（loudness 加 peak=true），<= -1 dBTP 才是真门禁；
    - 码率不当"下限"（暗色简洁内容本来就只有 0.5Mbps，当下限会天天误报）；
    - 字幕条数校验（expected_subtitles），这是"静默丢字幕"的唯一检查点。

    交付规格可配置（expect_w/expect_h/expect_fps）：死比 K.W/K.H/K.FPS 的话，
    720p/24fps 的合法交付会被报成"分辨率/帧率不符"——门禁判据跟交付规格脱节，
    比误报更糟的是它会让人去"修"一个根本没错的东西。缺省回落到模块常量。
    """
    errors, warns = [], []
    W, H, FPS = (int(expect_w or globals()["W"]), int(expect_h or globals()["H"]),
                 float(expect_fps or globals()["FPS"]))
    pr = probe(path)
    fmt = pr.get("format", {})
    streams = pr.get("streams") or [{}]
    vs = [s for s in streams if s.get("codec_type") == "video"] or [{}]
    au = [s for s in streams if s.get("codec_type") == "audio"]
    v = vs[0]
    if v.get("width") != W or v.get("height") != H:
        errors.append("分辨率 %sx%s != %dx%d" % (v.get("width"), v.get("height"), W, H))
    rate = str(v.get("r_frame_rate") or "")
    if rate and rate not in ("0/0",):
        try:
            num, _, den = rate.partition("/")
            got = float(num) / float(den or 1)
            if abs(got - float(FPS)) > 0.05:
                errors.append("帧率 %.3f != %g（SKILL 要求严格一致）" % (got, FPS))
        except Exception:
            pass
    if not au:
        errors.append("无音轨")
    else:
        sr = int(au[0].get("sample_rate") or 0)
        if sr and sr != SR:
            warns.append("音轨采样率 %d != %d" % (sr, SR))
    br = int(fmt.get("bit_rate") or 0)
    if min_bitrate and br and br < min_bitrate:
        warns.append("码率 %.2f Mbps 低于参考值 %.0f Mbps（暗色简洁内容可接受，看锐度比）"
                     % (br / 1e6, min_bitrate / 1e6))
    if fmt.get("duration") and expect_frames:
        d = float(fmt["duration"])
        if abs(d - expect_frames / float(FPS)) > 0.5:
            errors.append("时长 %.2fs 与预期 %.2fs 不符" % (d, expect_frames / float(FPS)))
    lo = loudness(path)
    if "I" not in lo:
        warns.append("未能测到积分响度（ebur128 解析失败），响度门禁本次未执行")
    if "I" in lo:
        if lo["I"] > -12:
            errors.append("响度 %.1f LUFS 过大（目标 -16 ~ -14）" % lo["I"])
        elif lo["I"] < -19:
            errors.append("响度 %.1f LUFS 过小（目标 -16 ~ -14）" % lo["I"])
        elif lo["I"] < -17.2 or lo["I"] > -14.5:
            warns.append("响度 %.1f LUFS 略偏（目标 -16 ± 1）" % lo["I"])
    if "TP" in lo:
        if lo["TP"] > -1.0:
            errors.append("真峰 %.1f dBTP 超限（目标 <= -1 dBTP）" % lo["TP"])
        elif lo["TP"] > -1.4:
            warns.append("真峰 %.1f dBTP 余量不足（AAC 编码后可能更高）" % lo["TP"])
    else:
        warns.append("未能测到真峰（ebur128 解析失败），责任门禁缺一项")
    # 黑帧阈值跟着主题底色走（见 detect_black 的注释）：底色亮度越低，阈值要越低，
    # 否则"深色主题的正常画面"会被当成整片黑屏。取底色亮度的 0.45 倍，
    # 下限 0.012（比纯黑略高，仍能抓到真黑），上限 0.10（ffmpeg 默认）。
    _pix_th = 0.10
    try:
        import stylekit as _ST
        _th = (theme or {}).get("bg") if isinstance(theme, dict) else None
        if isinstance(_th, (list, tuple)) and len(_th) >= 3:
            _luma = _ST.luminance(tuple(int(x) for x in _th[:3]))
            _pix_th = max(0.012, min(0.10, _luma * 0.45))
    except Exception:                                       # noqa: BLE001
        _pix_th = 0.10
    nb = detect_black(path, pix_th=_pix_th)
    if nb is None:
        warns.append("长黑帧检测未执行（ffmpeg blackdetect 失败），本项不能当作通过")
    elif nb > 0:
        warns.append("首尾之外检测到 %d 段长黑帧" % nb)
    ns = detect_silence(path)
    if ns is None:
        warns.append("长静音检测未执行（ffmpeg silencedetect 失败），本项不能当作通过")
    elif ns > 0:
        warns.append("检测到长静音段（确认是否有意留白）")
    if isinstance(theme, dict):
        # 主题体检的结论带进报告：这样"深色主题必然贴空窗阈值"这种事实不是靠嘴说
        lmax = float(theme.get("qa", {}).get("luma_max", 0.045))
        warns_t = ["主题 %s：空窗阈值 luma_max=%.3f、闪烁阈值 %.1f"
                   % (theme.get("label") or theme.get("id"), lmax,
                      float(theme.get("qa", {}).get("flicker_thresh", 6.0)))]
        warns.extend(warns_t)
    quality = None
    if ref_img is not None or (ref_png and os.path.exists(ref_png)):
        q = encode_quality(path, ref_png, ref_sec, ref_img=ref_img)
        quality = q
        if not q:
            if _cv2() is None:
                warns.append("没装 cv2（opencv-python-headless），锐度比门禁本次未执行——"
                             "这条门禁查的是「文字发不发虚」，装了才有；缺它不代表片子坏了，"
                             "但别把「没查」当成「通过了」")
            else:
                warns.append("锐度比计算失败（参考帧或抽帧不可读），编码锐度门禁本次未执行")
        if q:
            if q["ratio"] < 0.85:
                errors.append("编码损失偏大：锐度比 %.3f（<0.85 文字会发虚，降 CRF 或提 preset）"
                              % q["ratio"])
            elif q["ratio"] < 0.92:
                warns.append("编码锐度比 %.3f（0.85~0.92，可接受但可更好）" % q["ratio"])
    if expected_subtitles is not None:
        got = 0
        srt = os.path.join(os.path.dirname(os.path.abspath(str(path))), "..", "sub.srt")
        for cand in (notes.get("srt") if isinstance(notes, dict) else None,
                     str(notes) if isinstance(notes, str) else None,
                     srt):
            if cand and os.path.exists(str(cand)):
                got = len([b for b in open(str(cand), encoding="utf-8").read().strip().split("\n\n")
                           if b.strip()])
                break
        if not got:
            warns.append("没有找到 sub.srt（或它是空的），字幕条数门禁本次未执行")
        elif got != int(expected_subtitles):
            errors.append("字幕条数 %d != 剧本句数 %d（有字幕被静默丢弃）" % (got, expected_subtitles))
    return dict(errors=errors, warnings=warns, probe=pr, loudness=lo, quality=quality)
