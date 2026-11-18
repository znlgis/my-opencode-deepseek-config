# -*- coding: utf-8 -*-
"""voicekit.py — 旁白声线：直接给 edge voice id + 语气档 + 响度补偿表。

旁白不能全片一个人、一个语气念完：声线、语速、音高、音量与前后留白都要能逐句给，
并且随内容变化。只把 script.voice / rate / pitch 三个单值传给 filmkit.tts_batch，
全片就是同一个人用同一种语气念到底（听感即「旁白声音都差不多」）。

声线只认裸 edge voice id
------------------------
声线一律写 edge voice id（zh-CN-YunxiNeural），映射只发生一次、就在设计里。
多一层 persona 身份映射（写 narrator_male、真正下发给 edge-tts 的却是
zh-CN-YunyangNeural）时，任何一层没对齐，表现都是「配了却没生效」，
而且要等听成片才发现。

身份是数据，不该由一个正则或一张表决定：没有 persona 表，也没有
「文本里有引号就偷偷换人」那套规则。

每把音色的实测响度补偿**不能丢**：不补补偿，换音色就要人肉重新对电平，
正是要根除的「改了不生效 / 要人调」。所以补偿独立成一张按 voice id 的数据表
VOICE_LOUDNESS（没有任何身份含义）。

本模块只做「策展 + 计划」：给出每句用哪个音色、什么语气、语速/音高/音量抖多少、
前后留白多少。真正的合成仍在 filmkit.tts（Lead 负责接线），因此本模块
**不 import filmkit**（render.py 会同时 import 两者，互相 import 会成环），
只依赖 stylekit + 标准库。

三层结构
--------
  VOICE_LOUDNESS  响度补偿（数据）：每把音色实测的 level 补偿
  VOICE_TONES     6 档语气（怎么说）：相对 deltas + 前后留白 lead/tail
  _rules()        文本规则（说什么）：引号/数字/问叹/长短句/转折词 → 语气与微调

声线从哪来（高到低）
--------------------
  lines[].voice（显式 edge id）
  > script.voice.map / design.audio.voice.map 的 kind 映射
  > style_plan["voice"]["map"][kind]（风格包 / designkit.design_to_plan）
  > script.voice.id / design.audio.voice.id / style_plan["voice"]["default"]（主声线）
没有任何来源就报错（并给出迁移路径）——设计必须明确「谁在说话」。

声线一旦确定就**不再治理**：map 怎么写就怎么念，没有中间层在段边界偷偷换人、
也没有按预算限制同一部片出现几把声音。想少换人就少写几个 kind，不需要中间层猜意图。

确定性
------
所有抖动都来自 sha1(title|id|voice)，禁 random / time / hash()（str hash 带盐）。
同一份 film.json 两次 voice_plan() 完全相等；jitter=0 时与「无抖动」逐字段相等。

老结构
------
顶层 voice 单值与「没有 design 的 legacy 计划」都不受支持：voice_plan 直接抛
ValueError，消息里给三条可照抄的路径（design.audio.voice / lines[].voice / voice.map）。
不静默回落是刻意的：静默回落 = 配音变了却没人知道，比报错难查得多。

用法
----
    import voicekit as VK
    plan = VK.voice_plan(script, ST.resolve_script_style(script))
    VK.describe(plan)
    VK.spec_key(dict(plan[0], text=script["lines"][0]["text"]))   # 合成缓存键
    VK.calibrate_voice("zh-CN-YunxiNeural")   # 联网实测；离线 measured=False，不抛

    python voicekit.py --list                 # 音色响度表 + 语气档
    python voicekit.py --plan film.json       # 某份剧本的声线计划（可读表格）
    python voicekit.py --calibrate --only zh-CN-YunxiNeural   # 联网实测，打印该抄回的数
"""
from __future__ import annotations

import argparse
import array
import asyncio
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

# ============================================================================
# 一、响度补偿（数据，不是身份）：换音色不用手调电平
# ============================================================================
#
# 为什么这张表必须有：
#   九把音色的实测响度跨度 6.0 dB（-3.0 ~ +3.0）。差别来自音色本身（基频、气声、
#   端点侧的归一化），跟台词、语气、机器负载都无关，但**换一把音色就会整体偏几 dB**。
#   不补补偿，每次换音色都要试听、手调 gain —— 那正是「改了不生效 / 要人调」。
#
# 口径：gain_db = 目标响度 - 实测 rms_db，夹在 ±3 dB（正 = 该音色偏轻，mix 侧补回来）。
#   - 实测：2026-11-09 在本机用 edge-tts 7.3.1 合成同一句校准文本（CALIB_TEXT，21 个
#     汉字），ffmpeg 解成 24k 单声道，测 20ms 帧门限 RMS（低于 -45 dBFS 的静音帧不计）；
#   - 目标：CALIBRATION_LOG 里 9 条实测 rms_db 的中位数（-20.97 dBFS），由下方
#     CALIB_TARGET_DB 现算，不另写第二个魔数。
#
# **换机 / 换 edge-tts 版本 / 换 voice id 都要重测**：同一把音色在不同机器、不同版本上
# 差 1~2 dB 很常见，把上次的数字跨机照抄会一句响一句轻。重测入口：
#   python voicekit.py --calibrate（结果记 work/voice_calib.json，并打印该抄回的数）
VOICE_LOUDNESS = {
    "zh-CN-YunyangNeural": 3.0,           # 新闻/纪录片男声，实测最轻
    "zh-CN-XiaoxiaoNeural": -2.7,         # 标准女声，实测最响
    "zh-TW-HsiaoChenNeural": -3.0,        # 软糯女声（台湾腔），原始 -3.46，夹到 -3
    "zh-CN-XiaoyiNeural": -0.6,           # 活泼女声
    "zh-CN-YunjianNeural": 1.0,           # 有力男声
    "zh-CN-YunxiNeural": 1.9,             # 青年男声（最常用的旁白音色）
    "zh-CN-YunxiaNeural": 0.3,            # 少年音
    "zh-CN-liaoning-XiaobeiNeural": 0.0,  # 东北女声（本次校准的基准，补偿 0）
    "zh-CN-shaanxi-XiaoniNeural": -0.3,   # 陕西女声
}

# 表里没有的音色按 0 dB 处理（不猜），voice_plan 的 reason 会标 unmeasured_voice：
# 让人一眼看出「这句没补过」，而不是以为补过了。
CALIBRATED_ON = "2026-11-09"
CALIB_TEXT = "这是一句用来校准的中文样本，长度大约二十个字。"
CALIBRATION_LOG = {
    "zh-CN-YunyangNeural": {"dur": 4.56, "cps": 4.61, "rms_db": -24.39},
    "zh-CN-XiaoxiaoNeural": {"dur": 4.63, "cps": 4.53, "rms_db": -18.28},
    "zh-TW-HsiaoChenNeural": {"dur": 5.33, "cps": 3.94, "rms_db": -17.51},
    "zh-CN-XiaoyiNeural": {"dur": 4.73, "cps": 4.44, "rms_db": -20.40},
    "zh-CN-YunjianNeural": {"dur": 5.21, "cps": 4.03, "rms_db": -21.98},
    "zh-CN-YunxiNeural": {"dur": 4.80, "cps": 4.38, "rms_db": -22.91},
    "zh-CN-YunxiaNeural": {"dur": 4.85, "cps": 4.33, "rms_db": -21.27},
    "zh-CN-liaoning-XiaobeiNeural": {"dur": 5.47, "cps": 3.84, "rms_db": -20.97},
    "zh-CN-shaanxi-XiaoniNeural": {"dur": 5.09, "cps": 4.13, "rms_db": -20.63},
}


def _median(values):
    """中位数（偶数个取上中位）：定目标响度，不为了一个数去 import statistics。"""
    xs = sorted(float(v) for v in values)
    return xs[len(xs) // 2] if xs else 0.0


CALIB_TARGET_DB = round(_median(r["rms_db"] for r in CALIBRATION_LOG.values()), 2)

# ============================================================================
# 二、语气（怎么说）：deltas 叠在「裸 voice id」上；lead/tail 是基准行的偏移
# ============================================================================
# body 是基准行：所有 delta = 0，lead/tail 就是 BODY_LEAD / BODY_TAIL。
# voice_plan 只在这个基准上加减，所以「某一句听起来不对」时先看 reason 里是 tone:xxx
# 还是 rule:xxx，改一处就够，不用同时去翻音色表和文本规则。
BODY_LEAD = 0.62
BODY_TAIL = 0.92
TONE_IDS = ("body", "hook", "quote", "outro", "emphasis", "calm")

# gain_db 是**语气自己的**电平增量（强调句要更响），与 VOICE_LOUDNESS 的「音色补偿」
# 是两回事：前者是表演，后者是设备；voice_plan 里两者相加，各自都能单独解释。
VOICE_TONES = {
    "body": {"rate_delta": 0, "pitch_delta": 0, "volume_delta": 0, "gain_db": 0.0,
             "lead": 0.62, "tail": 0.92, "desc": "正文，平稳（基准行）"},
    "hook": {"rate_delta": 3, "pitch_delta": 1, "volume_delta": 2, "gain_db": 0.6,
             "lead": 0.48, "tail": 0.78, "desc": "开场，提一点速压一点留白，抓人"},
    "quote": {"rate_delta": -2, "pitch_delta": -1, "volume_delta": 1, "gain_db": 0.4,
              "lead": 0.70, "tail": 1.00, "desc": "引用，降速、留白，像另一个人在说话"},
    "outro": {"rate_delta": -3, "pitch_delta": -2, "volume_delta": 2, "gain_db": 0.8,
              "lead": 0.72, "tail": 1.02, "desc": "收尾，慢下来、沉下去"},
    "emphasis": {"rate_delta": 1, "pitch_delta": 2, "volume_delta": 4, "gain_db": 1.2,
                 "lead": 0.58, "tail": 0.86, "desc": "强调（数字/结论），抬音高加音量"},
    "calm": {"rate_delta": -5, "pitch_delta": -3, "volume_delta": 1, "gain_db": 0.5,
             "lead": 0.88, "tail": 1.25, "desc": "沉静，长留白，适合思辨/深夜"},
}

_RATE_LIMIT = (-15, 12)                  # 与 stylekit policy 的缺省一致
_PITCH_LIMIT = (-8, 8)
_VOL_LIMIT = (-10, 10)
_LEAD_LIMIT = (0.20, 1.60)
_TAIL_LIMIT = (0.20, 2.20)
_GAIN_LIMIT = 3.0

try:
    import stylekit as _ST
except Exception:                        # noqa: BLE001 — stylekit 缺失时只少了风格包这一层
    _ST = None


# ============================================================================
# 三、小工具：参数解析/格式化、汉字计数、抖动
# ============================================================================
# 允许小数（"+4.5%"）：edge-tts 只收整数百分比，所以解析后按 _round_half_away 取整。
# 正则只认整数时 "+4.5%" 解析失败 → 静默回落 0，而 0 与 "+0%" 在 spec_key 里
# 是**同一个 TTS 缓存键**，于是「把语速从 0 调到 4.5%」完全没生效，还复用了旧音频。
# 这是配置写对了却不生效里最难查的一类：没有报错、没有日志、听起来就是「没反应」。
_PCT_RE = re.compile(r"^\s*([+-]?)(\d+(?:\.\d+)?)\s*%\s*$")
_HZ_RE = re.compile(r"^\s*([+-]?)(\d+(?:\.\d+)?)\s*Hz\s*$")


def _round_half_away(v):
    """四舍五入而不是 Python 的银行家舍入：抖动量化要可预期、跨版本一致。"""
    v = float(v)
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


def parse_pct(v, default=0):
    """'+4%' -> 4；'-3%' -> -3；'+4.5%' -> 5（取整）；数字原样取整；解析失败回落 default。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(_round_half_away(v))
    m = _PCT_RE.match(str(v if v is not None else ""))
    if not m:
        return int(default)
    n = int(_round_half_away(float(m.group(2))))
    return -n if m.group(1) == "-" else n


def parse_hz(v, default=0):
    """'+2Hz' -> 2；'+2.5Hz' -> 3（取整）；解析失败回落 default。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(_round_half_away(v))
    m = _HZ_RE.match(str(v if v is not None else ""))
    if not m:
        return int(default)
    n = int(_round_half_away(float(m.group(2))))
    return -n if m.group(1) == "-" else n


def fmt_pct(v):
    """4 -> '+4%'；-3 -> '-3%'；edge-tts 只认整数百分比。"""
    return "%+d%%" % _round_half_away(v)


def fmt_hz(v):
    """2 -> '+2Hz'；edge-tts 只认整数 Hz。"""
    return "%+dHz" % _round_half_away(v)


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def hanzi_count(text):
    """数汉字（不含标点/数字/拉丁字母），长短句规则与 cps 都用它。"""
    return len(_CJK_RE.findall(str(text or "")))


def is_edge_voice(name):
    """形如 zh-CN-YunxiNeural 的原始 voice id（不是 persona 名 / 不是路径）。"""
    n = str(name or "").strip()
    return ("-" in n) and n.endswith("Neural")


def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _jitter(title, lid, who):
    """sha1(title|id|voice) 派生确定性抖动：rate/pitch/lead/tail + 两个量化阈值。

    rate/pitch 是浮点目标值（±1.5% / ±1Hz 再乘 policy.jitter），但 edge-tts 只收整数；
    若直接四舍五入，jitter=0.35 时 ±0.5 的小数会被吃光、抖动形同不存在。
    所以格式化前先用 _quantize() 做「确定性随机进位」，让长期期望值等于浮点目标。
    """
    h = hashlib.sha1(("%s|%s|%s" % (title, lid, who)).encode("utf-8")).hexdigest()

    def u(i, j):
        return int(h[i:j], 16) / float(65535 if j - i == 4 else 4294967295)

    return ((u(0, 8) * 2 - 1) * 1.5, (u(8, 16) * 2 - 1) * 1.0,
            (u(16, 24) * 2 - 1) * 0.04, (u(24, 32) * 2 - 1) * 0.04,
            u(32, 36), u(36, 40))


def _quantize(v, u):
    """确定性随机进位：floor(v)+1 的概率 = v 的小数部分，期望值仍是 v。

    u 来自 sha1（同一句永远同一结果），用来把浮点抖动量化成 edge-tts 能收的整数。
    v 本身是整数时（jitter=0）绝不进位，保证与「无抖动」逐字段相等。
    """
    base = int(math.floor(float(v)))
    frac = float(v) - base
    return base + (1 if (frac > 0 and frac > u) else 0)


# ============================================================================
# 四、文本规则（说什么）：全部确定性，命中即返回理由
# ============================================================================
# 引语起始符。**不含书名号《》**：正文里出现「《设计模式》」是书名，不是引用某人在说话。
# 把 《 算进引语，「今天我们聊《设计模式》」这句就会判成 quote 语气 ——
# 整句降速、留白加长，听感像突然换了频道（而这句根本没有人引语）。
_QUOTE_OPEN = ('"', "\u201c", "\u300c", "\u300e")
_QUOTE_WORDS = ("他说", "她说", "有人问", "所谓", "据称")
_NUM_UNITS = ("%", "万", "亿", "年", "倍")
_TURN_WORDS = ("但是", "然而", "不过", "其实", "问题是")


def _rules(text):
    """文本 -> {tone, rate, volume, tail, reasons}。

    优先级：引号 -> quote；数字/单位 -> emphasis（互斥，先命中先生效）。
    微调可叠加：问叹句 +2% 语速；>45 汉字 -2% 语速、尾巴 +0.10s；<12 汉字 +2% 语速；
    转折词 +2% 音量。所有判断只看字符串，不含任何随机。
    """
    t = str(text or "")
    out = {"tone": "", "rate": 0, "volume": 0, "tail": 0.0, "reasons": []}
    if any(q in t for q in _QUOTE_OPEN) or any(w in t for w in _QUOTE_WORDS):
        out["tone"] = "quote"
        out["reasons"].append("rule:quote")
    elif re.search(r"\d{3,}", t) or any(u in t for u in _NUM_UNITS):
        out["tone"] = "emphasis"
        out["reasons"].append("rule:num")
    if re.search(r"[？?！!][\u201d\u300d\u300f\"']?\s*$", t):
        out["rate"] += 2
        out["reasons"].append("rule:qmark")
    n = hanzi_count(t)
    if n > 45:
        out["rate"] -= 2
        out["tail"] += 0.10
        out["reasons"].append("rule:long")
    elif n < 12:
        out["rate"] += 2
        out["reasons"].append("rule:short")
    if any(w in t for w in _TURN_WORDS):
        out["volume"] += 2
        out["reasons"].append("rule:turn")
    return out


# ============================================================================
# 五、计划：每句用谁、什么语气、多快多响多长留白
# ============================================================================
def _resolve(script, style_plan):
    """拿风格计划：显式参数 > script["_style"]（render 已解析过）> stylekit 现算 > {}。

    返回 {} 不是故障：声线来源是 design.audio.voice，本模块自己就能读；
    stylekit 只负责风格包/节奏这一层，缺了它照样能出 voice 计划。
    """
    if isinstance(style_plan, dict) and style_plan:
        return style_plan
    cached = script.get("_style")
    if isinstance(cached, dict) and cached:
        return cached
    if _ST is not None:
        try:
            return _ST.resolve_script_style(script)
        except Exception:                # noqa: BLE001 — 坏剧本交给 filmcheck 报错
            pass
    return {}


def _design_voice(script):
    """读设计里的声线：返回 (主声线, {kind: id}, 策略 {auto, jitter})。

    为什么直接读 script["design"] 而不只依赖 style_plan：单测/验收会直接
    voice_plan(script)，此时没有 render.py 解析过的 _style；而 stylekit 对「没有
    design」的剧本已经直接报错，_resolve 只能返回 {}。design.audio.voice 是声线的
    唯一事实来源，能读就读，别要求调用方先跑一遍 render。

    策略跟着设计走（与 designkit.design_to_plan 对齐）：裸 id = 单声线、不抖动、
    不加文本规则；dict = 设计采样出来的多声线方案，auto=True、jitter 取设计值。
    本模块不 import designkit（那会多一层依赖），所以这三项在这里读一遍。
    """
    des = script.get("design") if isinstance(script.get("design"), dict) else {}
    au = des.get("audio") if isinstance(des.get("audio"), dict) else {}
    vo = au.get("voice")
    if isinstance(vo, str):
        return (vo.strip() or None), {}, {"auto": False, "jitter": 0.0}
    if not isinstance(vo, dict):
        return None, {}, {}
    mode = str(vo.get("mode") or "").strip()
    if mode == "wav":
        raise ValueError(
            "design.audio.voice.mode='wav'：voicekit 只出 edge-tts 的声线计划，"
            "wav 音色（录音/克隆）请直接交给混音侧，别从这里过。")
    vid = str(vo.get("id") or "").strip() or None
    raw = vo.get("map") if isinstance(vo.get("map"), dict) else {}
    vmap = {}
    for kind, v in raw.items():
        if isinstance(v, dict):
            v = v.get("id")
        if v not in (None, ""):
            vmap[str(kind)] = str(v).strip()
    try:
        jitter = max(0.0, float(vo.get("jitter", 0.0)))
    except (TypeError, ValueError):
        jitter = 0.0
    return vid, vmap, {"auto": True, "jitter": jitter}


def _script_voice(script):
    """script.voice 写成 dict 时的形状：返回 (主声线, {kind: id})。"""
    vo = script.get("voice")
    if not isinstance(vo, dict):
        return None, {}
    vid = str(vo.get("id") or vo.get("default") or "").strip() or None
    raw = vo.get("map") if isinstance(vo.get("map"), dict) else {}
    vmap = {str(k): str(v).strip() for k, v in raw.items() if v not in (None, "")}
    return vid, vmap


def _style_voice(sp):
    """style_plan["voice"]：返回 ({kind: id}, 主声线)。"""
    vsec = sp.get("voice") if isinstance(sp.get("voice"), dict) else {}
    raw = vsec.get("map") if isinstance(vsec.get("map"), dict) else {}
    vmap = {str(k): str(v).strip() for k, v in raw.items() if v not in (None, "")}
    default = str(vsec.get("default") or "").strip() or None
    return vmap, default


# 迁移说明只写一份：三处报错（非法声线 / 老结构 / map 值）都指向它，避免各处各写一套。
_MIGRATE = (
    "声线现在一律写 edge voice id（形如 zh-CN-YunxiNeural）。迁移任选一条：\n"
    "  1) design.json：audio.voice = {\"mode\": \"edge_id\", \"id\": \"zh-CN-YunxiNeural\"}；"
    "要分段换声线再加 map: {\"hook\": \"zh-CN-YunjianNeural\"}；\n"
    "  2) film.json：lines[].voice 逐句写 id；\n"
    "  3) 风格包 / voice.map：值改成 edge voice id。\n"
    "可用音色与实测响度补偿：python voicekit.py --list"
)


def _bad_voice(field, value):
    return ValueError("%s = %r 不是 edge voice id，而 voicekit 从 D3 起已删掉 persona 身份表。\n%s"
                      % (field, value, _MIGRATE))


def _old_structure(script, sp, scalar=None):
    """老结构（顶层 voice 单值 / 没有 design 的 legacy 计划）不受支持：直说怎么迁移。"""
    bits = []
    if scalar:
        bits.append("顶层 voice = %r，单值声线" % scalar)
    if sp.get("legacy"):
        bits.append("风格计划判定为 legacy（film.json 里没有 design/style）")
    return ValueError(
        "这份剧本还是老结构（%s），而 voicekit 从 D3 起已删掉 legacy / persona 路径。\n%s"
        % ("；".join(bits) or "没有 design.audio.voice，也没有 lines[].voice / voice.map", _MIGRATE))


def _validate_voice_map(field, vmap):
    for kind, vid in vmap.items():
        if not is_edge_voice(vid):
            raise _bad_voice("%s[%r]" % (field, kind), vid)


def _fmt_num(v):
    """计划里的 lead/tail 统一保留 3 位（避免 0.6200000000001 这种浮点尾巴）。"""
    return round(float(v) + 0.0, 3)


def _pick_tone(line, kind, rules, use_rules, reasons):
    """显式 tone > 文本规则 > kind 同名语气 > body。"""
    raw = line.get("tone")
    name = str(raw).strip() if raw not in (None, "") else ""
    if name in VOICE_TONES:
        reasons.append("tone:explicit")
        return name
    if name:
        reasons.append("unknown_tone:%s" % name)
    if use_rules and rules["tone"]:
        reasons.append("tone:%s" % rules["tone"])
        return rules["tone"]
    if kind in VOICE_TONES:
        return kind
    return "body"


def voice_plan(script, style_plan=None):
    """把剧本拆成逐句的旁白计划（纯函数、确定性，两次调用结果完全相等）。

    参数
    ----
    script      film.json 解析后的 dict（只读，不修改）
    style_plan  stylekit.resolve_script_style() 或 designkit.design_to_plan() 的返回；
                None 时本模块自己解析（design.audio.voice 优先，其次 stylekit）

    返回
    ----
    list[dict]，每行：id / kind / persona / tone / voice / rate / pitch / volume /
    gain_db / lead / tail / reason。rate/pitch/volume 是可直接透传 edge-tts 的字符串。
    persona 字段**保留但恒为 None**（老下游可能读它）；voice 恒为 edge voice id。
    """
    script = script if isinstance(script, dict) else {}
    sp = _resolve(script, style_plan)
    lines = [ln for ln in (script.get("lines") or [])]
    if not lines:
        return []                        # 空剧本给空计划；缺 lines 由 filmcheck 报
    vsec = sp.get("voice") if isinstance(sp.get("voice"), dict) else {}
    policy = vsec.get("policy") if isinstance(vsec.get("policy"), dict) else {}
    pace = sp.get("pacing") if isinstance(sp.get("pacing"), dict) else {}

    # ---- 声线来源：显式覆盖 > 设计 > 风格包（合并时后者先铺、前者覆盖）----
    s_vid, s_map = _script_voice(script)
    d_vid, d_map, d_pol = _design_voice(script)
    p_map, p_default = _style_voice(sp)
    vmap = {}
    vmap.update(p_map)
    vmap.update(d_map)
    vmap.update(s_map)
    default = s_vid or d_vid or p_default
    for field, m in (("script.voice.map", s_map), ("design.audio.voice.map", d_map),
                     ("style_plan.voice.map", p_map)):
        _validate_voice_map(field, m)
    for field, vid in (("script.voice.id", s_vid), ("design.audio.voice.id", d_vid),
                       ("style_plan.voice.default", p_default)):
        if vid and not is_edge_voice(vid):
            raise _bad_voice(field, vid)

    scalar = script.get("voice")
    has_line_voice = any(isinstance(ln, dict) and ln.get("voice") for ln in lines)
    if isinstance(scalar, str) and scalar.strip():
        # 顶层单值 voice。声线事实来源只有 design.audio.voice（或逐句 voice），
        # 再读它会和 design 打架，而「谁生效」取决于调用顺序 —— 最难查的一类。
        raise _old_structure(script, sp, scalar.strip())
    if not (default or vmap or has_line_voice):
        # 没有任何来源时**不猜**：静默套 kind 缺省会让整片悄悄换声线。
        # 宁可报错一次，让人把 design.audio.voice 补上。
        if isinstance(script.get("design"), dict):
            raise ValueError(
                "design.audio.voice 没有给出声线（id 与 map 都为空）：D3 起设计必须明确"
                "「谁在说话」。\n" + _MIGRATE)
        raise _old_structure(script, sp)

    # 策略：显式 style_plan 优先；没有（例如直接 voice_plan(script)）就用设计里的策略。
    # jitter 缺省 0.0 —— 抖动必须是设计采样出来的，不能由计划层凭空加。
    if not policy and d_pol:
        policy = d_pol
    jitter = max(0.0, float(policy.get("jitter", 0.0)))
    use_rules = _as_bool(policy.get("auto"), True)
    rate_lim = tuple(policy.get("rate_range") or _RATE_LIMIT)
    pitch_lim = tuple(policy.get("pitch_range") or _PITCH_LIMIT)
    base_lead = float(pace.get("lead", 0.55))
    base_tail = float(pace.get("tail", 0.85))
    title = str(script.get("title") or "")

    rows = []
    for i, ln in enumerate(lines):
        ln = ln if isinstance(ln, dict) else {}
        lid = str(ln.get("id") or ("l%02d" % (i + 1)))
        kind = str(ln.get("kind") or "body").strip() or "body"
        reasons = []
        rules = _rules(ln.get("text")) if use_rules else {
            "tone": "", "rate": 0, "volume": 0, "tail": 0.0, "reasons": []}

        # 身份只有一个来源，写错就报错，不「悄悄回落到某个缺省声线」：
        # 写错名会静默换人（听感变了但没有任何提示），是本项目最忌讳的坑。
        raw_p = ln.get("persona")
        if raw_p not in (None, ""):
            raise _bad_voice("lines[%d].persona" % i, raw_p)
        raw_v = ln.get("voice")
        if raw_v not in (None, ""):
            voice = str(raw_v).strip()
            if not is_edge_voice(voice):
                raise _bad_voice("lines[%d].voice" % i, raw_v)
            reasons.append("explicit:voice")
        else:
            mapped = vmap.get(kind, vmap.get("*"))
            if mapped:
                voice = mapped
                reasons.append("map:%s" % kind)
            elif default:
                voice = default
                reasons.append("default")
            else:
                raise ValueError(
                    "声线映射没覆盖 lines[%d].kind=%r，也没有全片主声线：不知道这句该谁念。\n"
                    "补 design.audio.voice.id（全片主声线）或 map[%r]。\n%s"
                    % (i, kind, kind, _MIGRATE))

        tone_name = _pick_tone(ln, kind, rules, use_rules, reasons)
        tone = VOICE_TONES[tone_name]

        # 音色补偿（设备）+ 语气补偿（表演）；表里没有的音色按 0 dB 并把事实写进 reason。
        gain = float(VOICE_LOUDNESS.get(voice, 0.0)) + float(tone["gain_db"])
        if voice not in VOICE_LOUDNESS:
            reasons.append("unmeasured_voice")

        rate_v = tone["rate_delta"] + rules["rate"]
        pitch_v = tone["pitch_delta"]
        vol_v = tone["volume_delta"] + rules["volume"]
        if use_rules:
            reasons.extend(rules["reasons"])

        lead = base_lead + (float(tone["lead"]) - BODY_LEAD)
        tail = base_tail + (float(tone["tail"]) - BODY_TAIL) + rules["tail"]

        u_rate, u_pitch = 0.0, 0.0
        if jitter > 0:
            jr, jp, jl, jt, u_rate, u_pitch = _jitter(title, lid, voice)
            rate_v += jr * jitter
            pitch_v += jp * jitter
            lead += jl * jitter
            tail += jt * jitter
            reasons.append("jitter:%.2f" % jitter)

        rate_c = _clamp(rate_v, rate_lim[0], rate_lim[1])
        pitch_c = _clamp(pitch_v, pitch_lim[0], pitch_lim[1])
        vol_c = _clamp(vol_v, _VOL_LIMIT[0], _VOL_LIMIT[1])
        if rate_c != rate_v:
            reasons.append("clamp:rate")
        if pitch_c != pitch_v:
            reasons.append("clamp:pitch")
        if _round_half_away(vol_c) != _round_half_away(vol_v):
            reasons.append("clamp:volume")

        # edge-tts 只收整数：jitter>0 时用确定性随机进位（长期期望=浮点目标），
        # jitter=0 时退化为纯四舍五入，保证与「无抖动」逐字段相等。
        if jitter > 0:
            rate_s = "%+d%%" % _quantize(rate_c, u_rate)
            pitch_s = "%+dHz" % _quantize(pitch_c, u_pitch)
        else:
            rate_s = fmt_pct(rate_c)
            pitch_s = fmt_hz(pitch_c)

        rows.append({
            "id": lid, "kind": kind, "persona": None, "tone": tone_name,
            "voice": voice, "rate": rate_s, "pitch": pitch_s,
            "volume": fmt_pct(vol_c), "gain_db": round(gain, 2),
            "lead": _fmt_num(_clamp(lead, _LEAD_LIMIT[0], _LEAD_LIMIT[1])),
            "tail": _fmt_num(_clamp(tail, _TAIL_LIMIT[0], _TAIL_LIMIT[1])),
            "reason": ";".join(reasons),
        })
    return rows


# ============================================================================
# 六、缓存键（v2：trim 进 key，否则改 trim 会静默复用旧音频）
# ============================================================================
def _norm_pct(v, default="+0%"):
    if v in (None, ""):
        return default
    return fmt_pct(parse_pct(v))


def _norm_hz(v, default="+0Hz"):
    if v in (None, ""):
        return default
    return fmt_hz(parse_hz(v))


def spec_key(spec):
    """v2 合成缓存键：sha1("v2|text|voice|rate|pitch|volume|trim|trim_db")[:16]。

    不复用旧的 tts_key（text|voice|rate|pitch）：那套键把 trim 漏在 key 外，
    同一句改 trim/trim_db 会静默复用旧音频。spec 传一行计划 + text 即可：
    spec_key(dict(plan[0], text=line["text"]))；trim/trim_db 缺省与 filmkit.tts 一致。
    """
    spec = spec if isinstance(spec, dict) else {}
    try:
        trim_db = float(spec.get("trim_db", -42.0))
    except (TypeError, ValueError):
        trim_db = -42.0
    trim = 1 if _as_bool(spec.get("trim", True), True) else 0
    raw = "v2|%s|%s|%s|%s|%s|%d|%.1f" % (
        str(spec.get("text") or ""), str(spec.get("voice") or ""),
        _norm_pct(spec.get("rate")), _norm_hz(spec.get("pitch")),
        _norm_pct(spec.get("volume")), trim, trim_db)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def plan_stats(plan):
    """计划摘要：用了几把声音 / 哪些语气（测试与日志用）。"""
    rows = [r for r in (plan or []) if isinstance(r, dict)]
    voices, tones = [], []
    for r in rows:
        v, t = r.get("voice"), r.get("tone")
        if v and v not in voices:
            voices.append(v)
        if t and t not in tones:
            tones.append(t)
    return {"lines": len(rows), "voices": voices, "tones": tones, "distinct": len(voices)}


# ============================================================================
# 七、人读输出
# ============================================================================
def describe(plan):
    """把计划表打成人可读的多行文本（id/kind/voice/tone/rate/pitch/vol/gain/lead/tail）。"""
    rows = [r for r in (plan or []) if isinstance(r, dict)]
    head = "%-8s %-7s %-28s %-9s %-6s %-6s %-6s %-6s %-6s %-6s" % (
        "id", "kind", "voice", "tone", "rate", "pitch", "vol", "gain", "lead", "tail")
    out = [head, "-" * len(head)]
    for r in rows:
        out.append("%-8s %-7s %-28s %-9s %-6s %-6s %-6s %+6.2f %-6.2f %-6.2f" % (
            str(r.get("id", "")), str(r.get("kind", "")), str(r.get("voice", "")),
            str(r.get("tone", "")), str(r.get("rate", "")), str(r.get("pitch", "")),
            str(r.get("volume", "")), float(r.get("gain_db", 0.0)),
            float(r.get("lead", 0.0)), float(r.get("tail", 0.0))))
    return "\n".join(out)


# ============================================================================
# 八、校准：真合成、真实测；离线优雅降级（不抛异常，不阻塞主链）
# ============================================================================
def _measure_wav(path, text=CALIB_TEXT):
    """读 24k 单声道 16-bit wav，返回 dur/cps/rms_db（纯标准库，不用 numpy）。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        if w.getsampwidth() != 2:
            raise ValueError("只接受 16-bit PCM wav（ffmpeg 要带 -sample_fmt s16）")
        raw = w.readframes(n)
    data = array.array("h")
    data.frombytes(raw)
    if sys.byteorder == "big":
        data.byteswap()
    total = len(data)
    dur = total / float(sr) if sr else 0.0
    fl = max(1, int(0.02 * sr))
    hop = max(1, int(0.01 * sr))
    frames = []
    for i in range(0, max(1, total - fl + 1), hop):
        seg = data[i:i + fl]
        if not seg:
            continue
        acc = 0
        for v in seg:
            acc += v * v
        frames.append(math.sqrt(acc / float(len(seg))) / 32768.0)
    gated = [x for x in frames if x > 10 ** (-45.0 / 20.0)]
    rms = math.sqrt(sum(x * x for x in gated) / len(gated)) if gated else 1e-6
    return {"dur": dur, "cps": hanzi_count(text) / dur if dur else 0.0,
            "rms_db": 20.0 * math.log10(rms + 1e-12)}


def _gain_for(rms_db):
    """实测 RMS -> 建议补偿（目标响度 - 实测，夹在 ±3 dB，保留 2 位）。"""
    return round(_clamp(CALIB_TARGET_DB - float(rms_db), -_GAIN_LIMIT, _GAIN_LIMIT), 2)


_CALIB_TIMEOUT = 30.0                    # 单条合成上限：没网时不能让主链挂死
_CALIB_CACHE = os.path.join("work", "voice_calib.json")
_CALIB_SCHEMA = 1


def _calib_path(cache_path=None):
    return str(cache_path or _CALIB_CACHE)


def _load_calib(cache_path):
    """读缓存 -> (校准文本, {voice: {rms_db, cps, dur}})；读不了/结构不对 -> (None, {})。"""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict) or data.get("schema") != _CALIB_SCHEMA:
        return None, {}
    voices = data.get("voices")
    return str(data.get("text") or ""), (voices if isinstance(voices, dict) else {})


def _store_calib(cache_path, text, row):
    """只缓存**成功测量**：失败也写会把一次断网固化成「测过」，之后永远读到假数据。"""
    _, voices = _load_calib(cache_path)
    voices = dict(voices)
    voices[row["voice"]] = {"rms_db": row["rms_db"], "cps": row["cps"], "dur": row["dur"]}
    # target_db 记下来只为审计（换机后一眼看出上次按多少 dBFS 补的）；
    # 读缓存时 gain 仍然用**当前** CALIB_TARGET_DB 现算，改目标不用清缓存。
    data = {"schema": _CALIB_SCHEMA, "text": text, "target_db": CALIB_TARGET_DB,
            "voices": voices}
    try:
        d = os.path.dirname(os.path.abspath(cache_path))
        os.makedirs(d, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError as e:                 # 缓存写不进去不影响本次结果，但要说出来
        return "%s: %s" % (type(e).__name__, e)
    return ""


async def _synthesize(edge_tts, text, voice, out_mp3):
    """合成一句校准文本；wait_for 是防「没网时挂死」的硬上限。"""
    await asyncio.wait_for(
        edge_tts.Communicate(text, voice).save(out_mp3), timeout=_CALIB_TIMEOUT)


def calibrate_voice(voice_id, cache_path=None, text=None):
    """实测一把音色的响度补偿；能联网才真测，其余情况优雅降级（绝不抛异常）。

    参数
    ----
    voice_id    edge voice id（zh-CN-YunxiNeural）
    cache_path  结果缓存，默认 work/voice_calib.json；同一把音色下次直接读
    text        校准文本，默认 CALIB_TEXT（改文本会让旧缓存失效并重测）

    返回 dict：
      测得  {'voice', 'rms_db', 'cps', 'dur', 'gain_db', 'measured': True[, 'cached': True]}
      降级  {'voice', 'rms_db': None, 'cps': None, 'gain_db': 表里的值,
             'measured': False, 'reason': '为什么没测成'}
    gain_db 沿用口径：CALIB_TARGET_DB - 实测 rms_db，夹在 ±3 dB。

    为什么要缓存：edge-tts 是免费端点，校准句要真联网合成；每句计划都去测一遍既慢
    也不稳定。但**只缓存成功测量**——失败也写缓存的话，一次断网就会把「没测」固化成
    「测过」，下一台机器照抄一个假增益，比没补还糟。
    """
    vid = str(voice_id or "").strip()
    text = str(text or CALIB_TEXT)
    cache = _calib_path(cache_path)
    base = {"voice": vid, "rms_db": None, "cps": None, "dur": None,
            "gain_db": float(VOICE_LOUDNESS.get(vid, 0.0)), "measured": False}

    if not is_edge_voice(vid):
        base["reason"] = ("不是 edge voice id：%r（persona 名已删除；写 zh-CN-XxxNeural）"
                          % vid)
        return base

    cached_text, voices = _load_calib(cache)
    hit = voices.get(vid) if cached_text == text else None
    try:
        rms = float(hit["rms_db"]) if isinstance(hit, dict) else None
    except (KeyError, TypeError, ValueError):
        rms = None
    if rms is not None:
        try:
            cps = float(hit.get("cps") or 0.0)
            dur = float(hit.get("dur") or 0.0)
        except (TypeError, ValueError):
            cps, dur = 0.0, 0.0
        base.update(rms_db=round(rms, 2), cps=round(cps, 2), dur=round(dur, 2),
                    gain_db=_gain_for(rms), measured=True, cached=True)
        return base

    try:
        import edge_tts
    except Exception as e:               # noqa: BLE001 — 没装就降级，不拖垮主链
        base["reason"] = "没装 edge_tts（%s: %s）" % (type(e).__name__, e)
        return base
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        base["reason"] = "没找到 ffmpeg，没法把 mp3 解成 PCM 测 RMS"
        return base

    tmp = tempfile.mkdtemp(prefix="voicekit_cal_")
    try:
        mp3 = os.path.join(tmp, "cal.mp3")
        wav = os.path.join(tmp, "cal.wav")
        coro = _synthesize(edge_tts, text, vid, mp3)
        try:
            asyncio.run(coro)
        except Exception as e:          # noqa: BLE001 — 没网/端点不可用都走这里
            # 事件循环有时根本起不来（asyncio.run 在 await 之前就抛），协程没人接；
            # 不关掉的话「never awaited」警告会盖住真正的原因，误导排查方向。
            coro.close()
            base["reason"] = ("合成失败（多半是没网或 edge 端点临时不可用）：%s: %s"
                              % (type(e).__name__, str(e)[:160]))
            return base
        try:
            r = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", mp3,
                                "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", wav],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "")[-200:])
            m = _measure_wav(wav, text)
        except Exception as e:          # noqa: BLE001 — 解码/测量失败同样降级
            base["reason"] = "ffmpeg 解码或测量失败：%s" % str(e)[:160]
            return base
        got = {"voice": vid, "rms_db": round(m["rms_db"], 2), "cps": round(m["cps"], 2),
               "dur": round(m["dur"], 2), "gain_db": _gain_for(m["rms_db"]),
               "measured": True}
        err = _store_calib(cache, text, got)
        if err:
            got["cache_error"] = err
        return got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def calibrate(voices=None, log=None):
    """逐把音色实测，打印「实测 vs 表里」，并给出该抄回 VOICE_LOUDNESS 的数。

    edge_tts 只在真测时 import（其余函数纯标准库，方便无网环境单测）。
    没装 edge_tts / 没 ffmpeg / 没网：逐条 SKIP，返回 0，绝不让主链失败。
    """
    log = log or (lambda s: None)
    ids = [str(v).strip() for v in (voices or list(VOICE_LOUDNESS)) if str(v).strip()]
    log("校准文本：%s（%d 个汉字）" % (CALIB_TEXT, hanzi_count(CALIB_TEXT)))
    log("目标响度：%.2f dBFS（CALIBRATION_LOG 实测中位数）；gain 夹在 ±%.1f dB"
        % (CALIB_TARGET_DB, _GAIN_LIMIT))
    log("")
    ok = 0
    for vid in ids:
        r = calibrate_voice(vid)
        if r["measured"]:
            ok += 1
            cur = float(VOICE_LOUDNESS.get(vid, 0.0))
            log("  OK   %-30s rms %7.2f dBFS  cps %.2f  gain %+6.2f  表 %+6.2f%s"
                % (vid, r["rms_db"], r["cps"], r["gain_db"], cur,
                   "（缓存）" if r.get("cached") else ""))
        else:
            log("  SKIP %-30s %s" % (vid, r.get("reason")))
    log("")
    if not ok:
        log("[SKIP] 没测到任何音色（多半是没网 / 没装 edge_tts / 没 ffmpeg）；"
            "VOICE_LOUDNESS 保持原样，主链不受影响")
        return 0
    log("把上面「gain」抄回 VOICE_LOUDNESS[voice]，并把 CALIBRATED_ON 改成今天；")
    log("换 voice id 必须重跑本命令 —— 不同音色的响度差 3-7 dB，凭感觉调会一句响一句轻。")
    return 0


# ============================================================================
# 九、CLI
# ============================================================================
def _setup_stdio():
    """Windows 控制台默认 GBK，中文/箭头会炸；能改就改，改不了不致命。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:               # noqa: BLE001
            pass


def _cli_list():
    print("voicekit 音色表（校准于 %s；edge-tts 免费端点只有 8 个 zh-CN 音色 + zh-TW/zh-HK）"
          % CALIBRATED_ON)
    print("gain_db = 目标 %.2f dBFS - 实测 RMS，夹在 ±%.1f dB（正 = 该音色偏轻，mix 侧补回来）"
          % (CALIB_TARGET_DB, _GAIN_LIMIT))
    print("%-30s %8s %9s %7s %7s" % ("voice", "gain_db", "实测RMS", "dur", "cps"))
    print("-" * 70)
    for vid, gain in VOICE_LOUDNESS.items():
        m = CALIBRATION_LOG.get(vid) or {}
        print("%-30s %+8.2f %9s %7s %7s" % (
            vid, gain,
            ("%.2f" % m["rms_db"]) if m else "-",
            ("%.2f" % m["dur"]) if m else "-",
            ("%.2f" % m["cps"]) if m else "-"))
    print("")
    print("表里没有的音色按 0 dB 处理（不猜），voice_plan 的 reason 会标 unmeasured_voice。")
    print("换机 / 换 edge-tts 版本 / 换 voice id 都要重测：python voicekit.py --calibrate")
    print("")
    print("语气档（body 是基准行：所有 delta = 0；lead/tail 相对它偏移）")
    print("%-10s %8s %9s %9s %8s %6s %6s  %s" % (
        "tone", "rate", "pitch", "volume", "gain_db", "lead", "tail", "desc"))
    print("-" * 120)
    for tid in TONE_IDS:
        t = VOICE_TONES[tid]
        print("%-10s %+8d %+9d %+9d %+8.2f %6.2f %6.2f  %s" % (
            tid, t["rate_delta"], t["pitch_delta"], t["volume_delta"], t["gain_db"],
            t["lead"], t["tail"], t["desc"]))
    print("")
    print("注：zh-TW-HsiaoChenNeural 是台湾腔软糯女声（微软标注 Warm）；要大陆标准音就换")
    print("    zh-CN-XiaoxiaoNeural，响度补偿按上表 -2.7 dB，其余字段不用动。")
    return 0


def _cli_plan(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            script = json.load(f)
    except (OSError, ValueError) as e:
        print("[剧本格式错误] %s: %s" % (path, e))
        return 2
    if not isinstance(script, dict) or not script.get("lines"):
        print("[剧本格式错误] %s 顶层必须是对象且含非空 lines" % path)
        return 2
    sp = _resolve(script, None)
    try:
        plan = voice_plan(script, sp)
    except ValueError as e:              # 配置错误要能照抄着改，不要 traceback
        print("[声线配置错误] %s" % e)
        return 2
    voce = sp.get("voice") if isinstance(sp.get("voice"), dict) else {}
    print("剧本：%s（%d 句）" % (path, len(plan)))
    print("风格：%s  voice.map=%s" % (sp.get("style") or "-",
                                      json.dumps(voce.get("map") or {}, ensure_ascii=False)))
    print("")
    print(describe(plan))
    print("")
    for r in plan:
        print("  %-8s voice=%-28s rate=%-5s pitch=%-6s vol=%-5s gain=%+5.2f"
              " lead=%-5.2f tail=%-5.2f %s"
              % (r["id"], r["voice"], r["rate"], r["pitch"], r["volume"], r["gain_db"],
                 r["lead"], r["tail"], r["reason"]))
    st = plan_stats(plan)
    print("")
    print("声线 %d 种：%s" % (st["distinct"], ", ".join(st["voices"]) or "-"))
    print("语气 %d 档：%s" % (len(st["tones"]), ", ".join(st["tones"]) or "-"))
    print("提示：同一份剧本两次 voice_plan() 结果完全相同；persona 字段恒为 None"
          "（只为兼容下游而保留）。")
    return 0


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(
        prog="voicekit.py", description="v1.0 旁白声线/语气计划（不合成，只出计划）")
    ap.add_argument("--list", action="store_true", help="列出音色响度表与语气档")
    ap.add_argument("--plan", metavar="FILM_JSON", help="打印某份 film.json 的声线计划")
    ap.add_argument("--calibrate", action="store_true",
                    help="联网实测各音色的 dur/RMS，打印建议 gain_db（失败 SKIP 返回 0）")
    ap.add_argument("--only", metavar="VOICE_ID",
                    help="--calibrate 时只测这些 edge voice id（逗号分隔）")
    a = ap.parse_args(argv)
    if a.list:
        return _cli_list()
    if a.calibrate:
        only = [x.strip() for x in (a.only or "").split(",") if x.strip()] or None
        if only:
            unknown = [x for x in only if not is_edge_voice(x)]
            if unknown:
                # 拼错名字会静默变成空集合 -> 打出「所有音色都合成失败（多半没网）」，
                # 把配置错误伪装成网络错误。这里先点名校验，给人话可照抄的可用清单。
                print("[未知音色] %s；可用：%s"
                      % ("、".join(unknown), " / ".join(VOICE_LOUDNESS)), file=sys.stderr)
                return 2
        return calibrate(only, log=print)
    if a.plan:
        return _cli_plan(a.plan)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
