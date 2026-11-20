# -*- coding: utf-8 -*-
"""qaasr.py — ASR 回读校验。

容器 / 响度 / 帧 / 音画同步这几层质检，都建立在一个假设上：
**成片音轨里念的就是剧本那几句话**。可是 TTS 缓存串了、换声线时漏了某句、
混音把某一段压没了、重新渲了但音轨是旧的——这些从容器和波形上都看不出来。
本模块把成片音轨转回文字，和剧本逐句对账：

1. 引擎优先级：faster-whisper（本机已装，import 失败才降级）> ffmpeg whisper 滤镜
   （ffmpeg 9.0.2-full_build 带 --enable-whisper，实测有 whisper 滤镜、没有 asr 滤镜）
   > 两者都不可用则 skipped。**不许把"没查"写成 pass**。
2. 匹配：按 timeline.json 每段 start/end 取 ASR 结果里落在该窗口内的文本，去标点 / 空白 /
   数字写法差异 / 常用繁体（whisper 中文输出常夹繁体）后做**字符级多重集召回**
   （中文没有词边界，词级召回不成立；多重集而不是集合，是因为"是"这种字重复出现要被计数）。
3. 偏移：报告每段 ASR 首字相对 timeline 段语音起点（voice_start，缺省用 start）的偏移；
   |偏移| > 0.35 s 给 warning。偏移只提示"对不齐"，不改召回率本身。
   ⚠ 没写 voice_start 的老 timeline 会退化成"相对段 start"，于是每句都多算一个 lead
   （本项目默认 0.55 s）→ 全片报偏移 warning。这是**判据的已知限制**，不是片子坏了：
   filmkit.build_timeline 写的 timeline 一定带 voice_start；只有手搓 timeline 才会缺。
4. 三档语义（与 gates.py 的 pass / fail / skipped 完全一致）：
     * 引擎跑不了（ffmpeg 缺失 / 无音轨 / 两个引擎都不可用 / 模型下载失败）-> skipped，
       errors 里写清原因，warnings 里写"没查不等于通过"；
     * recall < recall_warn（默认 0.72，与 render.py 的 G12 同口径）-> fail；
     * recall_warn <= recall < recall_pass（默认 0.85）-> pass + warning（别装没事）；
     * recall >= recall_pass -> pass。

为什么召回率用字符级多重集：本项目旁白是中文，whisper 的错字通常是同音字（"逐句"→"逐取"），
一字之差不该让整句判死；而"整句丢了"会直接让召回率掉一大截。字符级对中文天然合适。

为什么偏移要看 voice_start 而不是段 start：timeline 的 start 是"字幕/画面段起点"，
旁白在 lead 之后才开口，拿段 start 当期望首字时刻会系统性地多算一个 lead（默认 0.55 s），
把正常的对齐全部报成超差。

CLI：
    python qaasr.py <film.mp4> --timeline work/timeline.json [--script film.json]
                    [--model small] [--lang zh] [--limit-seconds 30]
                    [--out work/qa/asr.json] [--engine auto|faster-whisper|ffmpeg]
                    [--whisper-model <ggml-small.bin>] [--vad] [--strict]
    python qaasr.py <film.mp4> --timeline ... --asr-json <已有 ASR 结果.json>
                    # 离线回放，不加载模型（CI / 自测用）
    python qaasr.py --selftest
                    # 内置自检，不联网

依赖：stdlib + numpy（项目已有）；faster-whisper 可选（未装时自动降级）。
所有 ffmpeg / ffprobe 调用都走 FILMKIT_FFMPEG 环境变量覆盖 + fail-closed 探测
（候选文件存在 != 能用，必须真的跑通 "-version" 且 returncode=0）；
一个都不可用时返回空串，调用方必须按"没查"处理，不许回退成裸命令硬跑。

实测（本机：Windows 11 / Python 3.12.16 / CPU-only Radeon 890M / 59.6 GB 内存）：
  * 素材：3 句 edge-tts 中文旁白拼成的 19.87 s 成片（.team/asr_fixture/fixture.mp4，
    timeline 的 voice_start = 0.55 / 7.55 / 14.05）
  * faster-whisper tiny（int8）：加载 1.36 s，转写 0.50 s（RTF 0.025），召回 0.923，
    三句偏移 0.00 s（tiny 的段起点整秒截断，词级时间戳也只给整秒）
  * faster-whisper small（int8）：加载 2.0–3.3 s、转写 3.2–3.5 s（RTF 0.16–0.18）；
    召回 0.985（64/65，唯一的字是"鞋稿"错成"写稿"）；词级首字偏移
    -0.05 / -0.06 / -0.04 s（即 whisper 觉得旁白比 lead 早约 50 ms，属正常估计误差）
  * --limit-seconds 12（small）：召回 0.956（2 段 43/45），转写 3.56 s
  * ffmpeg whisper 滤镜（ggml-tiny.bin，use_gpu=0）：19.87 s 音频 3.75 s（RTF 0.19），
    召回 0.723（3 s 定长切块 + 尾部幻觉），偏移 0.00 / +0.02 / 0.00 s——只作后备引擎
  * 丢句反证：把 l03 从音轨里剪掉、timeline 仍写 3 句 -> small 召回 0.677、
    missing_lines=["l03"]、gate=fail、CLI exit 1
  * 确定性：tiny 同一输入跑两次（--limit-seconds 12）segments / offsets 完全一致
结论：CPU-only 跑 small 完全可行（20 s 片连加载约 6–8 s；30 s 片约 9–11 s）；
模型 484 MB（实测 HF 缓存 972 MB 是 fp16 + int8 两份），内存峰值 < 1 GB。
tiny 只适合快速冒烟（错字多、时间戳只到整秒）。默认 model_size="small" 就是这个原因。
"""

from __future__ import annotations

__version__ = "1.1.0"

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave

HERE = os.path.dirname(os.path.abspath(__file__))

# 同目录的 filmkit 提供 FFMPEG / ffprobe 解析；独立拷贝本文件时仍要能跑
try:
    import filmkit as _K
except Exception:  # noqa: BLE001
    _K = None

try:
    import numpy as np
except Exception:  # noqa: BLE001 —— 只有读 wav 用到
    np = None

# ---------------------------------------------------------------- 可调参数
# 抽取给 ASR 的统一音频格式：16 kHz 单声道（whisper 系列模型的输入口径）
AUDIO_SR = 16000
# 偏移告警阈值（秒）：旁白首字和 timeline 期望起点的差
OFFSET_WARN_S = 0.35
# 召回率的 pass / fail 线。**权威值在 render.py**（QA_ASR_RECALL_PASS / QA_ASR_RECALL_WARN，
# G12 门禁从这里读），本模块只是把默认值对齐，避免"同一部片、CLI 判 fail、门禁判 pass"：
# 同一事实只能有一个权威值。
# 为什么是 0.85 / 0.72 而不是 0.95 / 0.90：中文旁白实测（模板片，faster-whisper small）
# 干净成片召回只有 0.81–0.83 —— 中英混排专名（"DeepSeek 4.1 Flash"→DIXX 变体）是 ASR 的
# 正常误差，不是片子坏了；低于 0.72 才说明"整句丢了 / 音轨跟剧本不是一套"。
QA_RECALL_PASS = 0.85
QA_RECALL_WARN = 0.72
# 行级"疑似丢句"阈值：单行字符召回低于它，就进 missing_lines 让人复核。
# 注意：它**不参与门禁判定**（门禁只看全片字符召回），只是一个定位线索——
# 短句错一个字就可能掉到 0.8，拿全局 fail 线（0.72）去卡行级会满屏假阳性。
MISS_LINE_RECALL = 0.60
# 引擎名（进报告，别在别处硬编码字符串）
ENGINE_FW = "faster-whisper"
ENGINE_FF = "ffmpeg-whisper"
# 常用繁体 -> 简体（whisper 中文输出常夹繁体，实测 tiny 输出"視頻創作/從寫導到配音"）。
# 为什么仍是常用字表而不是全量 OpenCC：判据是**字符召回率**，表外的生僻写法差异会被召回率吸收；
# 而表越大维护成本越高——每个字对都要人工确认是不是一对一、有没有歧义（乾 / 著 / 瞭 / 幹 这类
# 一字多形刻意不收：映射错一个字反而会制造假差异，比漏收更伤判据）。
# 覆盖口径：日常高频字 + 本技能领域词（画面 / 编码 / 剪辑 / 配音 / 混音 / 门禁 / 工程）里的繁体写法。
# 验收口径不是"收了多少字"，而是真实 whisper 繁体输出的召回（两组样例实测）：
#   0.857 -> 0.929（"端刀端冒煙測試"：煙 归一后只剩 到/刀 这一个真实听错）
#   0.800 -> 0.933（"整條鏈路"：條/鏈 归一后只剩 质/致 这一个真实听错）
# 剩下的差异是 ASR 真错字（到听成刀、质听成致），**不该被繁简映射掩盖**——那正是 G12 要报的东西。
# 格式：pair 用空格分隔，每对两个字（繁体 -> 简体）；同一个繁体不许映射到两个不同简体
# （冲突检查放在 .tmpcheck 的校验脚本里跑，不塞进运行时：建表循环每对只做一次 dict 赋值）。
_T2S_PAIRS = """
創创 寫写 導导 動动 於于 單单 檢检 還还 對对 們们 這这 個个 時时 間间 軸轴 會会 樣样 麼么
為为 開开 關关 說说 讀读 聽听 產产 術术 數数 據据 應应 該该 讓让 給给 與与 從从 見见 覺觉
東东 車车 長长 門门 問问 題题 話话 語语 詞词 義义 體体 點点 線线 網网 頁页 風风 飛飞 馬马
魚鱼 鳥鸟 麗丽 龐庞 謝谢 傳传 統统 實实 現现 發发 員员 專专 業业 務务 內内 兩两 準准 確确
認认 識识 環环 節节 圖图 檔档 標标 質质 進进 過过 運运 邊边 際际 復复 雜杂 處处 將将 軍军
醫医 藥药 價价 錢钱 買买 賣卖 貨货 資资 費费 華华 國国 陸陆 續续 斷断 歸归 隨随 離离 難难
響响 較较 願愿 齊齐 魯鲁 爾尔 牆墙 殘残 殺杀 氣气 漢汉 溫温 濕湿 燈灯 燒烧 熱热 嗎吗
視视 頻频 頭头 無无 萬万 種种 學学 聲声 靜静 連连 結结 構构 證证 驗验 試试 測测 範范 圍围
錢钱 銀银 鋼钢 鐵铁 鐘钟 鍵键 盤盘 碼码 絡络 紀纪 錄录 訊讯 號号 頭头 髮发 隻只 醜丑 龍龙
鳳凤 寶宝 貴贵 團团 黨党 齡龄 麵面 麼么 麼么

畫画 幀帧 濾滤 鏡镜 豎竖 橫横 攝摄 劇剧 場场 轉转 編编 譜谱 樂乐 軌轨 採采 疊叠 蓋盖 顯显 隱隐
軟软 設设 選选 項项 劃划 組组 織织 類类 庫库 瀏浏 覽览 鏈链 腳脚 裝装 載载 夾夹 刪删 複复 製制
貼贴 鈕钮 擊击 滾滚 雙双 觸触 頂顶 預预 優优 簡简 階阶 級级 態态 狀状 變变 換换 緩缓 衝冲 壓压
縮缩 擴扩 減减 適适 順顺 邏逻 輯辑 緒绪 夠够 擇择 滿满 討讨 穩稳 機机 報报 憑凭 簽签 壞坏 裡里
裏里 兒儿 幾几 塊块 條条 煙烟 達达 並并 週周 臺台 灣湾 聯联 繫系 係系 書书 繞绕 總总 顆颗 張张
筆笔 記记 憶忆 閉闭 啟启 執执 欄栏 嚴严 須须 講讲 談谈 後后 誰谁 訪访 戶户 習习 練练 愛爱 歡欢
親亲 師师 強强 淨净 潔洁 衛卫 護护 廢废 棄弃 電电 權权 陰阴 陽阳 雲云 銳锐 參参 議议 評评 暫暂
繼继 儲储 匯汇 遲迟 帶带 寬宽 細细 圓圆 歲岁 維维 終终 飽饱 區区 遺遗 補补 揚扬 眾众 衆众 觀观
調调 輸输 錯错 誤误 規规 則则 緣缘 決决 丟丢 論论 來来 請请 幫帮 舊旧 險险 傷伤 樓楼 橋桥 園园
趕赶 經经 謂谓 驟骤 漸渐 飯饭 雞鸡 豬猪 貓猫 購购 銷销 幣币 稅税 併并 餘余
"""
_T2S = {}
for _pair in _T2S_PAIRS.split():
    if len(_pair) == 2:
        _T2S[_pair[0]] = _pair[1]

# 数字写法归一：阿拉伯数字 -> 中文逐位写法（"2024"->"二零二四"、"第3季度"->"第三季度"）。
# 选这个方向而不是反过来，是因为单字用例（"3"->"三"）反过来做不了——中文数字转阿拉伯
# 需要区分"三"是数字还是"一起"里的量词，而逐位映射是确定性的、不会误伤。
# 已知盲区：数量词写法（"3000" 读作"三千"）会归一成"三零零零"和"三千"对不上；
# 这种差异只会丢极少数几个字的召回，不影响整句判定，也不值得为它引入词表。
_CN_DIGITS = "零一二三四五六七八九"
_ARABIC_RUN = re.compile(r"\d+")


# ---------------------------------------------------------------- ffmpeg / ffprobe
_FFMPEG_CACHE = None  # None = 还没探测；"" = 探测过，一个都不可用（fail-closed）
_FFPROBE_CACHE = None
_FFMPEG_FILTERS_CACHE = None


def _probe_exe(exe) -> str:
    """候选可执行文件是否真的能用：必须跑通 "<exe> -version" 且 returncode == 0。

    fail-closed：文件不存在 / 不是可执行 / 权限拒绝 / 超时 / 非 0 退出，一律返回 ""。
    探测阶段吞掉异常是必须的——探测失败只代表"这个候选不可用"，不该把 import 炸掉。
    """
    try:
        if not exe:
            return ""
        exe = str(exe).strip().strip('"').strip("'")
        if not exe:
            return ""
        cand = exe
        if not os.path.isfile(cand):
            cand = shutil.which(exe) or ""
            if not cand:
                return ""
        p = subprocess.run([cand, "-version"], capture_output=True, timeout=15)
        if p.returncode != 0:
            return ""
        return cand
    except Exception:  # noqa: BLE001
        return ""


def _ffmpeg() -> str:
    """解析可用的 ffmpeg 可执行文件；一个都不可用返回 ""（调用方必须当"没查"）。

    顺序：FILMKIT_FFMPEG 环境变量 > filmkit.FFMPEG > PATH > WinGet/系统固定路径。
    每个候选都要真的跑通 -version 才算数（见 _probe_exe）。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE
    cands = [os.environ.get("FILMKIT_FFMPEG")]
    if _K is not None:
        try:
            cands.append(getattr(_K, "FFMPEG", None))
        except Exception:  # noqa: BLE001
            pass
    cands.append(shutil.which("ffmpeg"))
    local = os.environ.get("LOCALAPPDATA", "")
    user = os.environ.get("USERPROFILE", "")
    if local:
        cands.append(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    if user:
        cands.append(
            os.path.join(
                user, "AppData", "Local", "Microsoft", "WinGet", "Links", "ffmpeg.exe"
            )
        )
    cands += ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
    seen, out = set(), ""
    for c in cands:
        key = os.path.normcase(str(c or ""))
        if not c or key in seen:
            continue
        seen.add(key)
        hit = _probe_exe(c)
        if hit:
            out = hit
            break
    _FFMPEG_CACHE = out
    return out


def _ffprobe() -> str:
    """解析 ffprobe：先找 ffmpeg 同目录的 ffprobe，再 PATH。找不到返回 ""。"""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE is not None:
        return _FFPROBE_CACHE
    cands = []
    ff = _ffmpeg()
    if ff:
        base = os.path.basename(ff)
        for name in (
            ("ffprobe.exe", "ffprobe")
            if base.lower().endswith(".exe")
            else ("ffprobe", "ffprobe.exe")
        ):
            cands.append(os.path.join(os.path.dirname(os.path.abspath(ff)), name))
    cands += [shutil.which("ffprobe"), "ffprobe"]
    seen, out = set(), ""
    for c in cands:
        key = os.path.normcase(str(c or ""))
        if not c or key in seen:
            continue
        seen.add(key)
        hit = _probe_exe(c)
        if hit:
            out = hit
            break
    _FFPROBE_CACHE = out
    return out


def _ffmpeg_has_whisper() -> bool:
    """本机 ffmpeg 是否带 whisper 滤镜（9.0.2-full_build 带 --enable-whisper，实测有）。

    只认"转写"滤镜：9.0.2 里没有 asr 滤镜（实测 -filters 只匹配到 whisper），
    所以别按 "asr" 去找——那样永远找不到，白白把后路断掉。
    """
    global _FFMPEG_FILTERS_CACHE
    if _FFMPEG_FILTERS_CACHE is not None:
        return _FFMPEG_FILTERS_CACHE
    ff = _ffmpeg()
    ok = False
    if ff:
        try:
            p = subprocess.run(
                [ff, "-hide_banner", "-filters"], capture_output=True, timeout=30
            )
            txt = (p.stdout or b"").decode("utf-8", "replace")
            txt += (p.stderr or b"").decode("utf-8", "replace")
            for line in txt.splitlines():
                s = line.strip()
                # 形如 ".. whisper           A->A       Transcribe audio using whisper.cpp."
                if " whisper " in (" " + s + " ") and "->" in s:
                    ok = True
                    break
        except Exception:  # noqa: BLE001
            ok = False
    _FFMPEG_FILTERS_CACHE = ok
    return ok


# ---------------------------------------------------------------- 文本归一化 / 召回
def _digits_to_cn(digits: str) -> str:
    """阿拉伯数字串 -> 中文逐位写法（"2024"->"二零二四"、"007"->"零零七"）。

    逐位而不是读成数值：中文旁白里年份（2024 读"二零二四"）、编号（"第007号"）
    都是逐位念的，而 CTranslate2/whisper 输出的也正是"二零二四"这种写法。
    读数值反而会引入"两千零二十四"这种没人会念的形式。
    """
    return "".join(_CN_DIGITS[int(ch)] for ch in str(digits) if ch.isdigit())


def _norm_text(text) -> str:
    """把一段文本归一化到"可比的字符序列"。

    做四件事，全部是**写法差异**，不改变语义：
      1. NFKC（全角空格/全角数字/连字等 -> 半角）；
      2. 去标点 / 空白 / 控制字符 / 符号（unicodedata 分类 P* / Z* / C* / S*；S* 是符号，
         "①"、"+" 这类也去，避免"加号"vs"+"的假差异）；
      3. 常用繁体 -> 简体（whisper 中文输出常夹繁体）；
      4. 数字写法归一：阿拉伯数字串 -> 中文逐位写法（"2024"->"二零二四"，
         "第3季度"->"第三季度"）。方向见 _CN_DIGITS 的注释。
    归一后长度可能为 0（纯标点行），调用方要能处理。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "Z", "C", "S"):
            continue  # 标点 / 分隔符 / 控制符 / 符号：不算内容
        out.append(_T2S.get(ch, ch))
    s = "".join(out)

    s = _ARABIC_RUN.sub(lambda m: _digits_to_cn(m.group(0)), s)
    return s


def _char_recall(ref_norm: str, hyp_norm: str) -> float:
    """字符级多重集召回：|ref ∩ hyp| / |ref|（Counter 计数交集，重复字按出现次数算）。

    ref_norm 已归一化；空 ref 返回 1.0（没有可查的内容，不制造假 FAIL——由调用方
    在**全片**层面判"没有可对照文本"并记 skipped）。
    """
    if not ref_norm:
        return 1.0
    if not hyp_norm:
        return 0.0
    from collections import Counter

    a, b = Counter(ref_norm), Counter(hyp_norm)
    hit = sum((a & b).values())
    return hit / float(len(ref_norm))


# ---------------------------------------------------------------- 时间轴
def _load_json_arg(obj, what="参数"):
    """接受 dict 或路径：dict 原样返回，路径读 JSON。读不了抛 ValueError（不吞）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    path = str(obj)
    if not os.path.exists(path):
        raise ValueError("%s 不存在：%s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception as e:  # noqa: BLE001
        raise ValueError("%s 解析失败（%s）：%s" % (what, path, e)) from e


def _scenes_of(tl, script=None) -> list:
    """从 timeline（+ 可选剧本）取出要对照的段落列表。

    剧本优先：lines[].text 是"原稿"，timeline 只是排轴产物；改过台词没重跑 layout 时
    两者会不一致——那种情况下应该拿**原稿**当基准，由召回率去暴露时间轴没跟上。
    缺 text 的段直接跳过（老版本 timeline 不一定带 text），但全部缺就报错。

    返回 [{"id","start","end","voice_start","voice_end","text"}]，按 start 排序。
    """
    if isinstance(tl, list):
        # 便利入口：直接传 scenes 列表（自测 / 只关心段落时用；契约上仍是 timeline 路径或 dict）
        scenes = [s for s in tl if isinstance(s, dict)]
    else:
        tl = _load_json_arg(tl, "timeline") or {}
        if not isinstance(tl, dict):
            raise ValueError("timeline 既不是 dict 也不是路径：%r" % (type(tl),))
        scenes = [s for s in (tl.get("scenes") or []) if isinstance(s, dict)]
    if not scenes:
        raise ValueError(
            'timeline 里没有 scenes（形如 {"scenes":[{id,start,end}, ...]}）'
        )
    by_id = {}
    if script:
        sc = _load_json_arg(script, "剧本") or {}
        for ln in sc.get("lines") or []:
            if isinstance(ln, dict) and ln.get("id") is not None:
                by_id[str(ln["id"])] = str(ln.get("text") or "")
    out = []
    for s in scenes:
        try:
            start = float(s.get("start"))
            end = float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        sid = str(s.get("id") if s.get("id") is not None else "s%02d" % (len(out) + 1))
        text = by_id.get(sid)
        if text is None:
            text = str(s.get("text") or "")
        row = {
            "id": sid,
            "start": start,
            "end": end,
            "text": text,
            "voice_start": None,
            "voice_end": None,
            # 词级首词的**绝对**时刻：偏移判据的首选期望值（见 _judge 里的三级来源）。
            # 不搬运它的话，QA 就只能拿 voice_start 当唯一真值，误报会一直存在。
            "words_abs": s.get("words_abs"),
        }
        # voice_start 是"旁白开口"的时刻；偏移判据以它为期望起点（见模块 docstring）
        for key in ("voice_start", "voice_end"):
            try:
                v = float(s.get(key))
            except (TypeError, ValueError):
                v = None
            if v is not None and math.isfinite(v):
                row[key] = v
        if row["voice_start"] is None:
            row["voice_start"] = row["start"]
        if row["voice_end"] is None:
            row["voice_end"] = row["end"]
        out.append(row)
    if not out:
        raise ValueError(
            "timeline 的 scenes 里没有任何可用段（需要数值 start/end 且 end > start）"
        )
    out.sort(key=lambda r: r["start"])
    return out


# ---------------------------------------------------------------- 音频抽取 / 探测
def _audio_info(film) -> dict:
    """探测成片音轨：{"has_audio","duration","sample_rate","probe_ok"}。ffprobe 缺失时用 ffmpeg -i 兜底。

    probe_ok 区分两种"has_audio=False"：**真的没有音轨** 与 **没有工具可以探测**。
    以前两者都按"成片没有音轨"报，工具缺失时错误归因到片子上（skipped 的档位不变，
    但原因写错了，排障会往错误方向走）。
    """
    info = {
        "has_audio": False,
        "duration": None,
        "sample_rate": None,
        "probe_ok": False,
    }
    film = str(film)
    fp = _ffprobe()
    if fp:
        info["probe_ok"] = True
        try:
            p = subprocess.run(
                [
                    fp,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,sample_rate:format=duration",
                    "-of",
                    "json",
                    film,
                ],
                capture_output=True,
                timeout=120,
            )
            if p.returncode == 0 and p.stdout:
                data = json.loads(p.stdout.decode("utf-8", "replace"))
                for st in data.get("streams") or []:
                    if str(st.get("codec_type")) == "audio":
                        info["has_audio"] = True
                        try:
                            info["sample_rate"] = (
                                int(st.get("sample_rate") or 0) or None
                            )
                        except (TypeError, ValueError):
                            pass
                        break
                try:
                    d = float((data.get("format") or {}).get("duration") or 0.0)
                    if d > 0:
                        info["duration"] = d
                except (TypeError, ValueError):
                    pass
        except Exception:  # noqa: BLE001
            pass
    if not info["has_audio"]:
        # ffprobe 缺失 / 没解析出音轨时用 ffmpeg -i 的 stderr 兜底：
        # 宁可多跑一次，也不能因为"没探测到音轨"就把有音轨的片子判 skipped
        ff = _ffmpeg()
        if ff:
            info["probe_ok"] = True
            try:
                q = subprocess.run(
                    [ff, "-hide_banner", "-i", film], capture_output=True, timeout=180
                )
                txt = (q.stderr or b"").decode("utf-8", "replace")
                if "Audio:" in txt:
                    info["has_audio"] = True
                if info["duration"] is None:
                    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
                    if m:
                        info["duration"] = (
                            int(m.group(1)) * 3600
                            + int(m.group(2)) * 60
                            + float(m.group(3))
                        )
            except Exception:  # noqa: BLE001
                pass
    return info


def _extract_wav(film, out_wav, *, limit_seconds=None) -> None:
    """把成片音轨抽成 16 kHz 单声道 s16 wav（两个引擎共用一份输入，保证同输入同结果）。"""
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError(
            "找不到可用的 ffmpeg（FILMKIT_FFMPEG / PATH 都没有跑得通的 ffmpeg）"
        )
    cmd = [ff, "-v", "error", "-nostdin", "-y"]
    if limit_seconds:
        cmd += ["-t", "%.3f" % float(limit_seconds)]
    cmd += [
        "-i",
        str(film),
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SR),
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    p = subprocess.run(cmd, capture_output=True, timeout=1800)
    if (
        p.returncode != 0
        or not os.path.exists(out_wav)
        or os.path.getsize(out_wav) < 1000
    ):
        raise RuntimeError(
            "抽取音轨失败（%s）：%s"
            % (film, (p.stderr or b"").decode("utf-8", "replace")[-500:])
        )


def _read_wav_f32(path):
    """读 16 kHz 单声道 s16 wav -> (numpy float32 数组, 采样率)。只认 PCM16。"""
    if np is None:
        raise RuntimeError("numpy 不可用，读不了 wav")
    with wave.open(str(path), "rb") as w:
        ch, sw, sr, n = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError("wav 采样宽度不是 16-bit（%d），请检查 ffmpeg 输出" % sw)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


# ---------------------------------------------------------------- 引擎：faster-whisper
_FW_IMPORT = None  # None = 没试过；True/False = 结果
_FW_IMPORT_ERR = ""


def _faster_whisper_available() -> bool:
    """faster_whisper 能否 import（不加载模型、不联网）。"""
    global _FW_IMPORT, _FW_IMPORT_ERR
    if _FW_IMPORT is not None:
        return _FW_IMPORT
    try:
        import faster_whisper  # noqa: F401

        _FW_IMPORT = True
    except Exception as e:  # noqa: BLE001
        _FW_IMPORT = False
        _FW_IMPORT_ERR = "%s: %s" % (type(e).__name__, e)
    return _FW_IMPORT


def _model_cache_bytes(model_size):
    """faster-whisper 模型的本地缓存大小（HF hub 目录，排除 .incomplete 半截文件）。

    只用于报告"用了多大的模型"，查不到返回 None（不猜数字）。
    """
    root = os.path.join(
        os.path.expanduser("~"),
        ".cache",
        "huggingface",
        "hub",
        "models--Systran--faster-whisper-%s" % model_size,
    )
    if not os.path.isdir(root):
        return None
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".incomplete"):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total or None


def _asr_faster_whisper(
    wav, *, model_size="small", lang="zh", vad=False, log=print
) -> dict:
    """faster-whisper 转写。

    注意：**不要用 transcribe(文件路径)**。本机 faster-whisper 1.2.1 + 新版 PyAV 不兼容，
    它的 decode_audio 会 TypeError（av.open() got an unexpected keyword 'metadata_errors'）。
    所以这里统一走"ffmpeg 抽 wav -> numpy float32 -> transcribe(数组)"，既绕开 av 的坑，
    又不引入新依赖（numpy 项目已有）。

    确定性：temperature=0、condition_on_previous_text=False。CPU int8 下多线程浮点归约
    理论上可能有极小抖动（同一句偶发一字之差），实测两次结果一致，但没有穷举验证——
    报告里不承诺位级确定性，只承诺"不影响门禁结论"。
    """
    from faster_whisper import WhisperModel

    t0 = time.time()
    model = WhisperModel(str(model_size), device="cpu", compute_type="int8")
    load_s = time.time() - t0
    audio, sr = _read_wav_f32(wav)
    if sr != AUDIO_SR:
        raise RuntimeError("wav 采样率 %d != %d，抽取参数不对" % (sr, AUDIO_SR))
    audio_s = audio.size / float(AUDIO_SR)
    if log:
        log(
            "   [ASR] faster-whisper %s：加载 %.2fs，音频 %.2fs，开始转写…"
            % (model_size, load_s, audio_s)
        )
    t1 = time.time()
    seg_iter, info = model.transcribe(
        audio,
        language=str(lang or "zh"),
        beam_size=5,
        temperature=0.0,
        vad_filter=bool(vad),
        condition_on_previous_text=False,
        # 词级时间戳只为拿"这段第一个字到底落在哪一秒"。实测 segment.start 会被
        # whisper 的 1 s/2 s 栅格吃掉（模板片 19.87 s 出来的是 0 / 7 / 14，全整秒），
        # 拿它算偏移只会得到 0.00 的假精确；词的 start 是真实对齐的。
        word_timestamps=True,
    )
    segs = []
    for s in seg_iter:
        txt = str(getattr(s, "text", "") or "").strip()
        try:
            a, b = float(s.start), float(s.end)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        seg = {"start": round(a, 3), "end": round(b, 3), "text": txt}
        try:
            words = list(getattr(s, "words", None) or [])
        except Exception:  # noqa: BLE001
            words = []
        if words:
            try:
                w0 = float(getattr(words[0], "start", float("nan")))
                if math.isfinite(w0):
                    seg["first_word_t"] = round(w0, 3)
            except (TypeError, ValueError):
                pass
        segs.append(seg)
    asr_s = time.time() - t1
    return {
        "engine": ENGINE_FW,
        "model": str(model_size),
        "segments": segs,
        "load_s": round(load_s, 2),
        "asr_s": round(asr_s, 2),
        "audio_s": round(audio_s, 3),
        "model_bytes": _model_cache_bytes(model_size),
        "lang": str(getattr(info, "language", lang) or lang),
        "lang_prob": round(float(getattr(info, "language_probability", 0.0) or 0.0), 3),
        "detail": "faster-whisper %s / CPU int8%s"
        % (model_size, " / vad" if vad else ""),
    }


# ---------------------------------------------------------------- 引擎：ffmpeg whisper
def _default_whisper_models(model_size) -> list:
    """ffmpeg whisper 滤镜用的 ggml 模型候选（本地文件，**本工具不自动下载**）。

    为什么不自动下载：whisper.cpp 的 ggml 模型和 faster-whisper 的 CTranslate2 模型不是
    一个格式，自动下 75–500 MB 的二进制属于"眼睛之外的动作"；这里只探测，找不到就
    让 ffmpeg 引擎不可用（fail-closed），由用户 --whisper-model 或环境变量显式指定。

    候选顺序：QAASR_WHISPER_MODEL / FILMKIT_WHISPER_MODEL > <cwd 及向上 3 层>/{models,
    work/models,tools/whisper}/ggml-<size>.bin。
    """
    out = []
    for env in ("QAASR_WHISPER_MODEL", "FILMKIT_WHISPER_MODEL"):
        v = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if v:
            out.append(v)
    roots = []
    try:
        d = os.path.abspath(os.getcwd())
    except OSError:
        d = ""
    for _ in range(4):
        if not d:
            break
        roots.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    name = "ggml-%s.bin" % model_size
    for root in roots:
        for sub in (
            "models",
            os.path.join("work", "models"),
            os.path.join("tools", "whisper"),
        ):
            out.append(os.path.join(root, sub, name))
    # 本项目约定：<SKILL_DIR>/assets/models/ggml-<size>.bin
    out.append(os.path.join(HERE, "..", "assets", "models", name))
    out.append(os.path.join(HERE, "models", name))
    return out


def _find_whisper_model(model_size, explicit=None) -> str:
    """找 ffmpeg whisper 用的 ggml 模型文件；找不到返回 ""。"""
    cands = []
    if explicit:
        cands.append(str(explicit))
    cands += _default_whisper_models(model_size)
    for c in cands:
        try:
            if c and os.path.isfile(c) and os.path.getsize(c) > 1_000_000:
                return os.path.abspath(c)
        except OSError:
            continue
    return ""


def _asr_ffmpeg_whisper(wav, *, model_path, lang="zh", log=print) -> dict:
    """用 ffmpeg 的 whisper 滤镜转写（后备引擎，实测可用但比 faster-whisper 慢约 20 倍）。

    两个实测踩过的坑，代码里必须处理：
      1. **Windows 盘符冒号**：滤镜参数是 key=value:key=value，路径里的 "C:" 会被当成
         参数分隔符。各种反斜杠转义写法在 ffmpeg 9.0.2 上实测直接报
         "No option name near ..."（滤镜解析器不认 C: 这种转义）。最稳的办法是
         在一个临时目录里跑，模型和音频都用**相对文件名**，destination 也写相对名。
      2. **输出格式**：format=json 时 destination 文件是**每行一个 JSON 对象**（不是一个大
         数组），start/end 单位是**毫秒整数**，且按 3 s 定长窗口切块、窗口之间会重叠。
         这里逐行解析，遇到解析不了的行直接跳过并计数（不静默：返回 parse_skipped）。
    """
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("找不到可用的 ffmpeg")
    if not _ffmpeg_has_whisper():
        raise RuntimeError(
            "本机 ffmpeg 没有 whisper 滤镜（需要 --enable-whisper 的构建）"
        )
    model_path = os.path.abspath(str(model_path))
    tmp = tempfile.mkdtemp(prefix="qaasr_ff_")
    try:
        # 模型与音频用相对名放临时目录：只为了让滤镜参数里没有盘符冒号
        target_model = os.path.join(tmp, "model_ggml.bin")
        try:
            os.link(model_path, target_model)
        except OSError:
            shutil.copyfile(model_path, target_model)
        local_wav = os.path.join(tmp, "in.wav")
        shutil.copyfile(str(wav), local_wav)
        dest = "asr_out.json"
        af = (
            "whisper=model=model_ggml.bin:language=%s:translate=0:use_gpu=0:format=json:"
            "destination=%s" % (str(lang or "zh"), dest)
        )
        t0 = time.time()
        p = subprocess.run(
            [
                ff,
                "-v",
                "info",
                "-nostats",
                "-nostdin",
                "-y",
                "-i",
                "in.wav",
                "-af",
                af,
                "-f",
                "null",
                "-",
            ],
            cwd=tmp,
            capture_output=True,
            timeout=3600,
        )
        wall = time.time() - t0
        out_path = os.path.join(tmp, dest)
        if p.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(
                "ffmpeg whisper 滤镜失败（rc=%s）：%s"
                % (p.returncode, (p.stderr or b"").decode("utf-8", "replace")[-600:])
            )
        segs, bad = [], 0
        with open(out_path, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    a = float(row.get("start")) / 1000.0  # 毫秒 -> 秒
                    b = float(row.get("end")) / 1000.0
                    txt = str(row.get("text") or "").strip()
                except Exception:  # noqa: BLE001
                    bad += 1
                    continue
                if math.isfinite(a) and math.isfinite(b) and txt:
                    segs.append({"start": round(a, 3), "end": round(b, 3), "text": txt})
        audio_s = None
        try:
            with wave.open(local_wav, "rb") as w:
                audio_s = round(w.getnframes() / float(w.getframerate()), 3)
        except Exception:  # noqa: BLE001
            pass
        return {
            "engine": ENGINE_FF,
            "model": os.path.basename(model_path),
            "segments": segs,
            "load_s": None,
            "asr_s": round(wall, 2),
            "audio_s": audio_s,
            "model_bytes": os.path.getsize(model_path),
            "lang": str(lang or "zh"),
            "lang_prob": None,
            "parse_skipped": bad,
            "detail": "ffmpeg whisper 滤镜（ggml %s，use_gpu=0）"
            % os.path.basename(model_path),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 片段 -> 段落归属
def _sentence_windows(scenes) -> list:
    """每段的"语音窗口"（句子级别）：voice_start..voice_end，缺省 start..end。"""
    out = []
    for sc in scenes:
        a = float(
            sc.get("voice_start") if sc.get("voice_start") is not None else sc["start"]
        )
        b = float(sc.get("voice_end") if sc.get("voice_end") is not None else sc["end"])
        if b <= a:
            a, b = float(sc["start"]), float(sc["end"])
        out.append((a, b))
    return out


def _assign_text(scenes, segments) -> list:
    """把 ASR 片段按时间窗归属到 timeline 段落，返回每段 {"text","first_t","anchor"}。

    归属规则（**按音节在语音窗口里的占比裁文本**，不是"整段归给重叠最多的段"）：
      whisper 的 segment 边界很粗（faster-whisper 按语音活动切、ffmpeg whisper 干脆 3 s
      定长切），一个片段常常横跨两句旁白。整段归给最大重叠段会把上一句的尾字算进下一句，
      既污染召回率也污染偏移。这里假设"一个片段内的字在它的语音区间里匀速说出"，
      把片段语音区间与该片段所有重叠窗口按时间铺开，字符按比例分给各段：

          T = Σ 重叠时长；第 j 个字（共 n 个）的虚拟位置 = j / n × T；
          落在第 i 段窗口内那一段位置的字符就归第 i 段。

      这是"没有强制对齐信息"时最合理的近似，也是本项目能接受的口径——
      召回率对少量边界错配不敏感（丢一个字 = 掉 1/n），而偏移另由 first_t 单独估。
    first_t 是该段 ASR 首字的估算时刻（窗口起点 + 该字在窗口内的偏移）。
    anchor 记录来源片段下标（-1 = 没有重叠片段）。
    """
    n = len(scenes)
    acc = [
        {"text": "", "first_t": None, "anchor": -1, "seg_start": None} for _ in range(n)
    ]
    if not segments:
        return acc
    wins = _sentence_windows(scenes)
    for si, seg in enumerate(segments):
        try:
            a, b = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
            continue
        chars = [ch for ch in str(seg.get("text") or "") if not ch.isspace()]
        if not chars:
            continue
        # 词级首字时刻（faster-whisper 给），没有就退回按比例估（ffmpeg whisper 没有词级）
        try:
            first_word_t = float(seg.get("first_word_t"))
            if not math.isfinite(first_word_t):
                first_word_t = None
        except (TypeError, ValueError):
            first_word_t = None
        overlaps = []
        for i, (wa, wb) in enumerate(wins):
            lo, hi = max(a, wa), min(b, wb)
            if hi - lo > 1e-6:
                overlaps.append((i, lo, hi, hi - lo))
        if not overlaps:
            continue
        overlaps.sort(key=lambda r: r[1])
        total = sum(r[3] for r in overlaps)
        cnt = len(chars)
        cum = 0.0
        for i, lo, _hi, dur in overlaps:
            before = cum  # 本窗口之前的累计重叠时长
            c0 = int(round(before / total * cnt)) if total > 0 else 0
            cum += dur
            c1 = int(round(cum / total * cnt)) if total > 0 else cnt
            c0 = max(0, min(c0, cnt))
            c1 = max(c0, min(c1, cnt))
            piece = "".join(chars[c0:c1])
            if not piece:
                continue
            acc[i]["text"] += piece
            if acc[i]["anchor"] >= 0:
                continue
            # 首字时刻：优先用词级时间戳（真实对齐）；没有就按"字在窗口内匀速"估
            off = (c0 / float(cnt)) * total - before if total > 0 else 0.0
            off = max(0.0, min(float(dur), off))
            if first_word_t is not None and c0 == 0:
                acc[i]["first_t"] = first_word_t
                # 片段起点就是本段首字：把片段起点一并记下来（判偏移时要区分
                # 「whisper 把上一句尾音并进本片段」与「真的音画不同步」）。
                acc[i]["seg_start"] = a
            else:
                acc[i]["first_t"] = lo + off
                acc[i]["seg_start"] = None
            acc[i]["anchor"] = si
    return acc


def _load_words_abs(words_json=None):
    """读词级首词绝对时刻 -> {scene_id: sec}；读不到返回空 dict。

    数据来源与曾经踩的坑：
      * `work/words.json` 里的 `words[].start` 是**段内相对**时间，直接当绝对秒用会得到
        「偏差 -202s」这种荒谬结论（实测过）。
      * 真正可用的是 `timeline.json` 的 `scenes[].words_abs`（含本段首词的绝对时刻），
        它由 render 的 layout 阶段算好；`words.json` 的 `lines[].start` 是段起点，
        两者相加也能还原，但只有前者是权威值。
    所以这里只认 `words_json` 里**带绝对时间**的 `words_abs`（或调用方直接给的映射）；
    都没有就返回空 dict，调用方退回 voice_start 的旧口径——绝不拿相对时间去凑。
    """
    if not words_json:
        return {}
    try:
        if isinstance(words_json, dict):
            data = words_json
        else:
            with open(str(words_json), encoding="utf-8") as fh:
                data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, dict):
        return {}
    out = {}
    for lid, row in lines.items():
        if not isinstance(row, dict):
            continue
        ws = row.get("words_abs")  # 只认绝对时间；没有就不猜
        if not isinstance(ws, (list, tuple)) or not ws:
            continue
        first = ws[0]
        try:
            if isinstance(first, dict):
                t = first.get("start")
            else:
                t = first
            out[str(lid)] = float(t)
        except (TypeError, ValueError):
            continue
    return out


def _judge(
    scenes,
    segments,
    *,
    words_abs=None,
    recall_pass=QA_RECALL_PASS,
    recall_warn=QA_RECALL_WARN,
    offset_warn=OFFSET_WARN_S,
) -> dict:
    """纯函数：由段落 + ASR 片段算出召回率 / 缺句 / 偏移 / 三档结论。

    单独拎出来是为了 --selftest 和 --asr-json 离线回放能**不加载模型**验证判分逻辑：
    引擎只是"产生片段"的手段，判分对错必须和引擎解耦，否则没网/没模型时门禁就没法测。
    """
    recall_pass = float(recall_pass)
    recall_warn = float(recall_warn)
    if not (0.0 < recall_warn <= 1.0) or not (0.0 < recall_pass <= 1.0):
        raise ValueError(
            "recall_pass / recall_warn 必须落在 (0, 1]，收到 %r / %r"
            % (recall_pass, recall_warn)
        )
    if recall_warn > recall_pass:
        raise ValueError(
            "recall_warn(%.3f) 不能大于 recall_pass(%.3f)" % (recall_warn, recall_pass)
        )
    from collections import Counter

    assigned = _assign_text(scenes, segments)
    per_line, missing, offsets, offset_ref = [], [], {}, {}
    offset_straddle = {}
    offset_uncertain = {}
    offset_ref_extra = {}
    tot_ref, tot_hit = 0, 0
    lost_counter = Counter()  # 全片"ref 有、ASR 没对上"的字符
    for sc, got in zip(scenes, assigned):
        ref = _norm_text(sc.get("text"))
        hyp = _norm_text(got["text"])
        lost = Counter(ref) - Counter(hyp) if ref else Counter()
        if lost:
            lost_counter.update(lost.elements())
        hit = sum((Counter(ref) & Counter(hyp)).values()) if ref else 0
        r = (hit / float(len(ref))) if ref else 1.0
        tot_ref += len(ref)
        tot_hit += min(hit, len(ref))
        per_line.append(
            {
                "id": sc["id"],
                "start": round(float(sc["start"]), 3),
                "end": round(float(sc["end"]), 3),
                "ref_chars": len(ref),
                "asr_chars": len(hyp),
                "hit_chars": hit,
                "recall": round(r, 4),
                "asr_text": got["text"][:200],
                # 单段掉分定位：这一段里"剧本有、ASR 没对上"的字（最多 6 个）
                "lost": ["%s×%d" % (ch, n) for ch, n in lost.most_common(6)]
                if lost
                else [],
            }
        )
        if ref and r < MISS_LINE_RECALL:
            missing.append(sc["id"])
        if got["first_t"] is not None and ref:
            vs = sc.get("voice_start")
            expected = float(vs if vs is not None else sc["start"])
            # 期望起点优先用**词级时间戳的首词**（words.json 的 words_abs）：它是 TTS 自己
            # 报的开口时刻，比 timeline 的 voice_start（由整句 mp3 时长反推）更接近真相。
            # 为什么必须这么改：实测一部竖屏片 l26 报 -1.20s 偏移，但 words.json 首词
            # 落在 voice_start 之后 0.14s、混音里能量起跳在 0.15s——真正的偏差是 whisper
            # 把上一句尾音算进了本段首字。用 voice_start 当唯一真值只会持续误报。
            # 期望开口时刻的三级来源（越靠前越权威）：
            #   ① 本段词级首词的**绝对**时刻（timeline.scenes[].words_abs[0].start）——
            #      TTS 自己报的开口时刻，不受 whisper 段起点估计误差影响；
            #   ② 调用方显式传的映射（自测/离线回放用）；
            #   ③ voice_start（整句 mp3 时长反推）——没有词级能力时的旧口径。
            wa = None
            was = sc.get("words_abs")
            if isinstance(was, (list, tuple)) and was:
                first_w = was[0]
                try:
                    wa = float(
                        first_w.get("start") if isinstance(first_w, dict) else first_w
                    )
                    offset_ref_extra[sc["id"]] = "words_abs"
                except (TypeError, ValueError):
                    wa = None
            if wa is None and words_abs:
                wa = words_abs.get(sc["id"])
                if wa is not None:
                    offset_ref_extra[sc["id"]] = "words_abs(arg)"
            if wa is not None:
                expected = float(wa)
            raw_off = round(float(got["first_t"]) - expected, 3)
            # 跨句片段的伪偏移：whisper 的 segment 边界比句子粗，一个片段常常从上一句
            # 尾音开始。此时「本段首字」的 first_t 就是片段起点，会系统性偏早一个
            # 尾音长度（实测 l26 -1.20s，而该段真正开口时刻与 voice_start 只差 0.07s）。
            # 判据要同时满足两条才降级，避免把真实的音画不同步也一起放过：
            #   ① 本段首字**就是**片段起点（seg_start 有值）；
            #   ② 片段起点比期望开口时刻早出阈值以上（早到不可能是同一句）。
            seg_start = got.get("seg_start")
            straddle = (
                seg_start is not None
                and float(seg_start) < expected - float(offset_warn) - 1e-9
            )
            # 第二种不可判：期望开口时刻**没有任何 ASR 片段覆盖**，而本段首个片段从更晚开始。
            # 意思是 ASR 把这一段开头的弱起/气口判成了非语音，片段起点不能代表开口时刻；
            # 此时「first_t - 期望」既可能是真的晚开，也可能是 ASR 边界切错。
            # 实测 l21：raw +0.70s（片段从 166.80 起，而期望 166.10），但混音能量在 166.20 就起跳了。
            # 判据要求「期望时刻没被覆盖 + 首个片段晚于期望 + 晚出阈值以上」，三者缺一不可。
            covered = any(
                float(sg["start"]) <= expected < float(sg["end"])
                for sg in segments or ()
                if sg.get("start") is not None and sg.get("end") is not None
            )
            # 只把「缺口不大」的算不可判：缺口 <= 2 倍阈值时，更像弱起被切掉；
            # 缺口更大（比如整句晚了 1.7s）说明是真错位，必须报出来。
            late_onset = (
                not covered
                and float(got["first_t"]) > expected + float(offset_warn)
                and float(got["first_t"]) - expected <= float(offset_warn) * 3.0 + 1e-9
            )
            if late_onset:
                # seg_start 可能为 None（片段没有词级时间戳、或首字不是片段起点）。
                # 这里只是**记录现场**，不能因为少一个可选字段就让整个判分崩掉：
                # 曾经 round(float(None)) 直接抛 TypeError，门禁变成"跑了就红"而不是"判了有结论"。
                offset_uncertain[sc["id"]] = {
                    "first_t": round(float(got["first_t"]), 3),
                    "seg_start": (
                        round(float(seg_start), 3) if seg_start is not None else None
                    ),
                    "expected": round(float(expected), 3),
                    "reason": "ASR 首字晚于片段起点，边界不可信",
                }
                offset_ref[sc["id"]] = "片段边界不清晰（偏移不可判）"
            elif straddle:
                offset_straddle[sc["id"]] = {
                    "first_t": round(float(got["first_t"]), 3),
                    "seg_start": round(float(seg_start), 3),
                    "expected": round(float(expected), 3),
                }
                offset_ref[sc["id"]] = "跨句片段（偏移不可判）"
            else:
                offsets[sc["id"]] = raw_off
            # 期望起点用的是哪个字段要如实说：voice_start 缺省（或等于 start）时就是 start。
            # setdefault 而不是直接赋值：上面两个"不可判"分支写的是**结论**，这里写的是
            # **来源**；直接覆盖会让结论静默消失（warnings 里只剩"偏 +0.70s"、"建议试听"没了）。
            offset_ref.setdefault(
                sc["id"],
                offset_ref_extra.get(
                    sc["id"],
                    "voice_start"
                    if vs is not None and abs(float(vs) - float(sc["start"])) > 1e-9
                    else "start",
                ),
            )
    recall = (tot_hit / float(tot_ref)) if tot_ref else 0.0
    warnings, errors = [], []

    for lid in missing:
        row = next((p for p in per_line if p["id"] == lid), None)
        if row is not None:
            warnings.append(
                "段落 %s 的字符召回只有 %.2f（%d/%d），疑似丢句/错词：ASR=%r"
                % (
                    lid,
                    row["recall"],
                    row["hit_chars"],
                    row["ref_chars"],
                    row["asr_text"][:60],
                )
            )
    # 判不了的段落要**说出来**，不能静默丢：观众若真觉得某句对不上，
    # 这条提示能直接告诉他是哪几句没法自动判。
    if offset_uncertain:
        warnings.append(
            "这些段落的偏移无法自动判定（ASR 在期望开口处没有片段覆盖，"
            "片段边界不可信）：%s —— 需人工试听确认"
            % "、".join(sorted(offset_uncertain)[:8])
        )
    for lid, dt in sorted(offsets.items()):
        if abs(float(dt)) > float(offset_warn) + 1e-9:
            warnings.append(
                "段落 %s 的旁白首字比 timeline 的 %s 偏 %+.2fs（阈值 ±%.2fs）："
                "音画/字幕可能对不齐（通常是 TTS 缓存串了或混音起点变了）"
                % (
                    lid,
                    offset_ref.get(lid, "voice_start"),
                    float(dt),
                    float(offset_warn),
                )
            )

    if recall < recall_warn - 1e-12:
        errors.append(
            "ASR 回读字符召回 %.3f 低于 fail 线 %.2f（原稿 %d 字，对上 %d 字）："
            "成片旁白与剧本不一致——先查 TTS 缓存 / 混音是否丢了句子"
            % (recall, recall_warn, tot_ref, tot_hit)
        )
        status = "fail"
    elif recall < recall_pass - 1e-12:
        # 只报"召回 0.813"没法修：实测掉分几乎全在中英混排专名（"DeepSeek 4.1 Flash"
        # 被听成 DIXX 之类）。这里把"剧本里有、成片里没对上"的字符按次数排出来，
        # 让 agent 知道该改哪几个词（改读法 / 加同音别名 / 换掉 ASR 不认的写法）。
        warnings.append(
            "ASR 回读字符召回 %.3f 低于 pass 线 %.2f（但高于 fail 线 %.2f）："
            "pass 但记 warning，别当完全没问题" % (recall, recall_pass, recall_warn)
        )
        if lost_counter:
            tips = "、".join(
                "%s×%d" % (ch, n) for ch, n in lost_counter.most_common(12)
            )
            warnings.append(
                "成片里没对上的字（ref 有 / ASR 无，按次数）：%s —— 多是专名读法，"
                "改剧本写法或在 film.json 的 qa.asr 放宽" % tips
            )
        worst = sorted(
            (p for p in per_line if p.get("lost")),
            key=lambda p: float(p.get("recall") or 0.0),
        )[:2]
        for p in worst:
            warnings.append(
                "最差段落 %s（召回 %.2f，%d/%d 字）：没对上 %s；ASR 实际读到 %r"
                % (
                    p["id"],
                    float(p["recall"]),
                    p["hit_chars"],
                    p["ref_chars"],
                    "、".join(p["lost"]),
                    p["asr_text"][:50],
                )
            )
        status = "pass"
    else:
        status = "pass"

    detail = "字符召回 %.3f（%d/%d 字，%d 段，缺句 %d 段）" % (
        recall,
        tot_hit,
        tot_ref,
        len(per_line),
        len(missing),
    )
    if offsets:
        worst = max(offsets.items(), key=lambda kv: abs(float(kv[1])))
        detail += "；最大偏移 %s %+.2fs" % (worst[0], float(worst[1]))
    return {
        "recall": round(recall, 4),
        "ref_chars": tot_ref,
        "hit_chars": tot_hit,
        "lost_top": [{"ch": ch, "n": n} for ch, n in lost_counter.most_common(20)],
        "per_line": per_line,
        "missing_lines": missing,
        "offsets": offsets,
        "offset_ref": offset_ref,
        "offset_warn_s": float(offset_warn),
        "offset_straddle": offset_straddle,
        "offset_uncertain": offset_uncertain,
        "gate": {
            "id": "asr",
            "title": "ASR 回读（原稿 vs 成片旁白）",
            "status": status,
            "detail": detail,
        },
        "warnings": warnings,
        "errors": errors,
    }


# ---------------------------------------------------------------- 报告
def _write_json(rep, out_json) -> None:
    """写报告 JSON（allow_nan=False：报告里出现 NaN 说明有算法 bug，宁可当场炸）。"""
    out_json = str(out_json)
    d = os.path.dirname(os.path.abspath(out_json))
    if d:
        os.makedirs(d, exist_ok=True)
    txt = json.dumps(rep, ensure_ascii=False, indent=1, allow_nan=False)
    with open(out_json, "w", encoding="utf-8") as fp:
        fp.write(txt + "\n")


def report(
    film_mp4,
    tl,
    script=None,
    *,
    model_size="small",
    lang="zh",
    out_json=None,
    recall_pass=QA_RECALL_PASS,
    recall_warn=QA_RECALL_WARN,
    log=print,
    offset_warn=OFFSET_WARN_S,
    words_json=None,
    limit_seconds=None,
    engine="auto",
    asr_json=None,
    whisper_model=None,
    vad=False,
) -> dict:
    """成片旁白的 ASR 回读校验，返回可直接进门禁的 dict。

    参数
      film_mp4      成片路径（有音轨即可，视频轨不参与）
      tl            timeline：路径或 dict，需有 scenes[{id,start,end,(voice_start,voice_end,text)}]
      script        可选剧本（film.json 或其 dict）：给了就优先用 lines[].text 作为原稿
      model_size    faster-whisper 模型（tiny/base/small/medium/large-v3；默认 small）
      lang          ASR 语言（默认 zh）
      out_json      报告 JSON 输出路径（可选）
      recall_pass   >= 它 -> pass（默认 0.85，与 render.py 的 G12 同口径）
      recall_warn   < 它 -> fail；介于两者 -> pass + warning（默认 0.72）
      offset_warn   段落 ASR 首字相对 timeline 语音起点的偏移告警阈值（默认 0.35 s）
      limit_seconds 只对前 N 秒音轨做 ASR（自测提速用；timeline 会被同步裁剪）
      engine        "auto"（默认，faster-whisper 优先）/ "faster-whisper" / "ffmpeg"
      asr_json      已有 ASR 结果（路径或 dict，形如 {"segments":[{start,end,text}]}）：
                    给了就直接回放，不加载模型、不抽音频——CI / 自测 / 排查用
      whisper_model ffmpeg whisper 滤镜用的 ggml 模型路径（缺省按 _default_whisper_models 探测）
      vad           是否开 faster-whisper 的 VAD（默认关：静音段幻觉文本不影响字符召回，
                    而开 VAD 多一个模型要下载，破坏"零网络也能跑"）

    返回字段：judged / engine / recall / per_line / missing_lines / offsets / offset_ref /
    gate{status} / warnings / errors，以及耗时与模型信息。
    """
    t_start = time.time()
    film = str(film_mp4)
    rep = {
        "schema": "qaasr/1",
        "version": __version__,
        "film": film,
        "judged": False,
        "engine": None,
        "engine_detail": None,
        "model_size": str(model_size),
        "model_bytes": None,
        "lang": str(lang or "zh"),
        "audio_s": None,
        "limit_seconds": (float(limit_seconds) if limit_seconds else None),
        "load_s": None,
        "asr_s": None,
        "elapsed_s": 0.0,
        "recall": None,
        "ref_chars": 0,
        "hit_chars": 0,
        "recall_pass": float(recall_pass),
        "recall_warn": float(recall_warn),
        "offset_warn_s": float(offset_warn),
        "per_line": [],
        "missing_lines": [],
        "offsets": {},
        "offset_ref": {},
        "asr_segments": [],
        "gate": {
            "id": "asr",
            "title": "ASR 回读（原稿 vs 成片旁白）",
            "status": "skipped",
            "detail": "未执行",
        },
        "warnings": [],
        "errors": [],
    }

    def finish() -> dict:
        rep["elapsed_s"] = round(time.time() - t_start, 2)
        if out_json:
            try:
                _write_json(rep, out_json)
            except Exception as e:  # noqa: BLE001
                rep["warnings"].append("报告 JSON 写出失败（%s）：%s" % (out_json, e))
        if log:
            try:
                _log_summary(rep, log)
            except Exception:  # noqa: BLE001
                pass
        return rep

    # ---- 0. 前置条件：成片存在、timeline 可用 ----
    if not os.path.exists(film):
        rep["errors"].append("成片不存在：%s" % film)
        rep["gate"]["detail"] = "成片不存在，ASR 回读没有执行"
        rep["warnings"].append("ASR 回读**没有执行**，这不等于通过")
        return finish()
    try:
        scenes = _scenes_of(tl, script)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append("时间轴不可用：%s" % e)
        rep["gate"]["detail"] = "时间轴不可用，ASR 回读没有执行"
        rep["warnings"].append("ASR 回读**没有执行**，这不等于通过")
        return finish()
    if limit_seconds:
        lim = float(limit_seconds)
        if lim <= 0:
            raise ValueError("limit_seconds 必须 > 0（收到 %r）" % (limit_seconds,))
        clipped = []
        for sc in scenes:
            if float(sc["start"]) >= lim - 1e-9:
                continue
            row = dict(sc)
            row["end"] = min(float(sc["end"]), lim)
            row["voice_end"] = min(float(sc["voice_end"]), lim)
            if row["end"] - row["start"] <= 1e-6:
                continue
            clipped.append(row)
        scenes = clipped
        if not scenes:
            rep["errors"].append("limit_seconds=%.3f 把时间轴裁空了" % lim)
            rep["gate"]["detail"] = "时间轴被裁空，ASR 回读没有执行"
            rep["warnings"].append("ASR 回读**没有执行**，这不等于通过")
            return finish()
    ref_chars_total = sum(len(_norm_text(sc.get("text"))) for sc in scenes)
    if ref_chars_total <= 0:
        rep["errors"].append("timeline/剧本里没有任何可对照的文本（去标点后为空）")
        rep["gate"]["detail"] = "没有可对照文本，ASR 回读没有执行"
        rep["warnings"].append("ASR 回读**没有执行**，这不等于通过")
        return finish()

    # ---- 1. 取 ASR 结果：离线回放 > 指定引擎 > auto ----
    asr = None
    audio_info = _audio_info(film)
    if not audio_info.get("has_audio"):
        if asr_json is not None:
            # 回放只验判分链路，不碰音轨：必须说清楚，别让人以为"音轨查过了"
            rep["warnings"].append(
                "离线回放模式不检查音轨：本片探测不到音轨，"
                "但判分用的是 --asr-json 里的片段（回放 != 成片有音轨）"
            )
        elif not audio_info.get("probe_ok"):
            # 探测工具不可用时 _audio_info 也只能返回 has_audio=False，但不能把它写成
            # "成片没有音轨"——那是把环境缺件错误归因到片子上。档位同样是 skipped，
            # 区别只在原因必须写对（排障方向完全不同）。
            rep["errors"].append(
                "既没有可用的 ffprobe / ffmpeg 探测音轨，也没有任何音轨信息：%s" % film
            )
            rep["gate"]["detail"] = (
                "音轨探测工具不可用，ASR 回读没有执行（skipped != 通过）"
            )
            rep["warnings"].append(
                "ASR 回读**没有执行**（缺 ffprobe/ffmpeg），这不等于通过"
            )
            return finish()
        else:
            rep["errors"].append("成片没有音轨：%s" % film)
            rep["gate"]["detail"] = "成片无音轨，ASR 回读没有执行（没有东西可转写）"
            rep["warnings"].append("ASR 回读**没有执行**（无音轨），这不等于通过")
            return finish()
    if asr_json is not None:
        data = _load_json_arg(asr_json, "asr_json")
        segs = data.get("segments") if isinstance(data, dict) else data
        if not isinstance(segs, list):
            rep["errors"].append("asr_json 里没有 segments 列表")
            rep["gate"]["detail"] = "asr_json 结构不对，ASR 回读没有执行"
            rep["warnings"].append("ASR 回读**没有执行**，这不等于通过")
            return finish()
        asr = {
            "engine": "replay",
            "model": None,
            "segments": [],
            "load_s": None,
            "asr_s": None,
            "audio_s": None,
            "model_bytes": None,
            "lang": str(lang or "zh"),
            "lang_prob": None,
            "detail": "回放已有 ASR 结果",
        }
        for s in segs:
            if not isinstance(s, dict):
                continue
            try:
                a, b = float(s.get("start")), float(s.get("end"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(a) and math.isfinite(b) and b > a:
                seg = {
                    "start": round(a, 3),
                    "end": round(b, 3),
                    "text": str(s.get("text") or ""),
                }
                # 报告里的 asr_segments 带 first_word_t（词级首字时刻），回放时要原样带上，
                # 否则"把 qaasr 自己的报告回放一遍"会丢掉词级精度、偏移退回按比例估。
                try:
                    fw = float(s.get("first_word_t"))
                    if math.isfinite(fw):
                        seg["first_word_t"] = round(fw, 3)
                except (TypeError, ValueError):
                    pass
                asr["segments"].append(seg)

    ff = _ffmpeg()
    if asr is None and not ff:
        rep["errors"].append(
            '找不到可用的 ffmpeg（FILMKIT_FFMPEG 与 PATH 里都没有能跑通 "ffmpeg -version" 的'
            "可执行文件），音轨抽不出来，ASR 回读没有执行"
        )
        rep["gate"]["detail"] = (
            "找不到可用的 ffmpeg，ASR 回读没有执行（skipped != 通过）"
        )
        rep["warnings"].append("ASR 回读**没有执行**（缺 ffmpeg），这不等于通过")
        return finish()

    if asr is None:
        fw_ok = _faster_whisper_available()
        use_fw = fw_ok and engine in ("auto", "faster-whisper", ENGINE_FW)
        whisper_path = _find_whisper_model(model_size, whisper_model)
        ff_whisper = _ffmpeg_has_whisper()
        use_ff = (
            bool(whisper_path)
            and ff_whisper
            and engine in ("auto", "ffmpeg", ENGINE_FF)
        )
        if not use_fw and not use_ff:
            why = []
            if not fw_ok:
                why.append(
                    "faster_whisper 不可用（%s）" % (_FW_IMPORT_ERR or "import 失败")
                )
            elif not use_fw:
                why.append("faster_whisper 可用但被 --engine 排除")
            if not ff_whisper:
                why.append("本机 ffmpeg 没有 whisper 滤镜")
            elif not whisper_path:
                why.append(
                    "找不到 ffmpeg whisper 用的 ggml 模型（用 --whisper-model 指定）"
                )
            else:
                why.append("ffmpeg whisper 可用但被 --engine 排除")
            rep["errors"].append("两种 ASR 引擎都不可用：%s" % "；".join(why))
            rep["warnings"].append("ASR 回读**没有执行**（没有可用引擎），这不等于通过")
            rep["gate"]["detail"] = (
                "没有可用的 ASR 引擎，回读没有执行（skipped != 通过）"
            )
            return finish()

        tmpdir = tempfile.mkdtemp(prefix="qaasr_")
        wav = os.path.join(tmpdir, "audio.wav")
        try:
            if log:
                log("== ASR 回读：%s" % os.path.basename(film))
            try:
                _extract_wav(film, wav, limit_seconds=limit_seconds)
            except Exception as e:  # noqa: BLE001
                # 抽不出音轨（音轨为空/损坏、写盘失败、编码不支持）也是"没查成"：
                # 三档语义里记 skipped + 原因。以前这里直接抛出去，render.py 的
                # catch-all 会把它记成 G12 **fail**——环境/媒资问题被说成"门禁自身失败"，
                # 而且 report() 承诺"总是返回 dict"的契约也断了。
                rep["errors"].append("音轨抽取失败（%s: %s）" % (type(e).__name__, e))
                rep["gate"]["detail"] = (
                    "音轨抽取失败，ASR 回读没有执行（skipped != 通过）"
                )
                rep["warnings"].append("ASR 回读**没有执行**（抽轨失败），这不等于通过")
                return finish()
            if use_fw:
                try:
                    asr = _asr_faster_whisper(
                        wav,
                        model_size=model_size,
                        lang=lang,
                        vad=vad,
                        log=log or (lambda *_a, **_k: None),
                    )
                except Exception as e:  # noqa: BLE001
                    msg = "faster-whisper 转写失败（%s: %s）" % (type(e).__name__, e)
                    if use_ff:
                        rep["warnings"].append(
                            msg + "；已降级用 ffmpeg whisper 滤镜重试"
                        )
                        if log:
                            log("   [WARN] " + msg + "，降级 ffmpeg whisper…")
                    else:
                        rep["errors"].append(msg + "；且没有 ffmpeg whisper 的后备模型")
                        rep["gate"]["detail"] = (
                            "faster-whisper 模型加载/下载失败，"
                            "ASR 回读没有完成（skipped）"
                        )
                        rep["warnings"].append("ASR 回读**没有完成**，这不等于通过")
                        return finish()
            if asr is None:
                try:
                    asr = _asr_ffmpeg_whisper(
                        wav, model_path=whisper_path, lang=lang, log=log
                    )
                except Exception as e:  # noqa: BLE001
                    # 同抽轨失败：后备引擎跑挂了 = 本次没查成，记 skipped 而不是抛异常。
                    rep["errors"].append(
                        "ffmpeg whisper 转写失败（%s: %s）" % (type(e).__name__, e)
                    )
                    rep["gate"]["detail"] = (
                        "ffmpeg whisper 引擎失败，ASR 回读没有完成（skipped）"
                    )
                    rep["warnings"].append("ASR 回读**没有完成**，这不等于通过")
                    return finish()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 2. 判分 ----
    rep["engine"] = asr.get("engine")
    rep["engine_detail"] = asr.get("detail")
    rep["asr_segments"] = asr.get("segments") or []
    rep["load_s"] = asr.get("load_s")
    rep["asr_s"] = asr.get("asr_s")
    rep["model_bytes"] = asr.get("model_bytes")
    if asr.get("audio_s"):
        rep["audio_s"] = asr.get("audio_s")
    else:
        rep["audio_s"] = audio_info.get("duration")  # 前面探测过，不重复跑 ffprobe
    # 期望开口时刻优先用词级时间戳（words.json 的 words_abs）：TTS 自己报的首词时刻
    # 比 timeline 的 voice_start（整句 mp3 时长反推）更接近真相。没开词级能力时退回
    # voice_start，行为与旧版完全一致。
    judged = _judge(
        scenes,
        rep["asr_segments"],
        words_abs=_load_words_abs(words_json),
        recall_pass=recall_pass,
        recall_warn=recall_warn,
        offset_warn=offset_warn,
    )
    for key in (
        "recall",
        "ref_chars",
        "hit_chars",
        "lost_top",
        "per_line",
        "missing_lines",
        "offsets",
        "offset_ref",
        "gate",
        "warnings",
        "errors",
    ):
        rep[key] = judged[key]
    if asr.get("parse_skipped"):
        rep["warnings"].append(
            "ffmpeg whisper 输出里有 %d 行解析不了（格式变了？已跳过）"
            % asr["parse_skipped"]
        )
    if not rep["asr_segments"]:
        rep["errors"].append(
            "ASR 没有转出任何文字（音轨可能整段静音/只有音乐）："
            "剧本有 %d 个字要对上，召回按 0 处理" % rep["ref_chars"]
        )
        rep["gate"]["status"] = "fail"
        rep["gate"]["detail"] = "ASR 输出为空（召回 0），旁白可能整段丢了"
    rep["judged"] = True
    return finish()


def _log_summary(rep, log=print) -> None:
    """把报告打印成人能读的中文摘要（风格与 qa_frames 的 _print_summary 一致）。"""
    g = rep.get("gate") or {}
    st = g.get("status")
    tag = {"pass": "[PASS]", "fail": "[FAIL]", "skipped": "[SKIP]"}.get(st, "[??]")
    log("%s %s — %s" % (tag, g.get("title", "ASR 回读"), g.get("detail", "")))
    if rep.get("engine"):
        parts = ["引擎 %s（%s）" % (rep.get("engine"), rep.get("engine_detail") or "")]
        if rep.get("audio_s"):
            parts.append("音频 %.2fs" % float(rep["audio_s"]))
        if rep.get("asr_s") is not None:
            parts.append("转写 %.2fs" % float(rep["asr_s"]))
        if rep.get("load_s") is not None:
            parts.append("加载模型 %.2fs" % float(rep["load_s"]))
        log("   " + "，".join(parts))
    if rep.get("recall") is not None:
        log(
            "   字符召回 %.3f（pass 线 %.2f / fail 线 %.2f），缺句 %d 段"
            % (
                float(rep["recall"]),
                float(rep["recall_pass"]),
                float(rep["recall_warn"]),
                len(rep.get("missing_lines") or []),
            )
        )
    for e in (rep.get("errors") or [])[:6]:
        log("   [ERROR] %s" % e)
    for w in (rep.get("warnings") or [])[:6]:
        log("   [WARN] %s" % w)


# ---------------------------------------------------------------- 自检（不联网）
def _make_tiny_mp4(path, *, seconds=2.0, with_audio=True) -> bool:
    """造一个极小的 mp4（lavfi color + anullsrc），自检用；造不出来返回 False。

    不联网、不需要素材：只验证"有音轨/无音轨"两条路径的探测与抽轨。
    """
    ff = _ffmpeg()
    if not ff:
        return False
    cmd = [
        ff,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x36:r=10:d=%.2f" % seconds,
    ]
    if with_audio:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono:d=%.2f" % seconds,
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            "-shortest",
        ]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        return p.returncode == 0 and os.path.exists(path)
    except Exception:  # noqa: BLE001
        return False


def selftest() -> int:
    """内置自检：**不联网、不加载 ASR 模型**（用 --asr-json 离线回放路径测判分）。

    覆盖：文本归一化、字符召回、三档判定边界、偏移告警、跨句片段归属、
    引擎全不可用时 skipped、无音轨 skipped、CLI 端到端、import 无副作用。
    """
    results = []
    t_start = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix="qaasr_st_")

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        st = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        results.append((name, st, detail))
        print("  [%s] %-36s %s" % (st, name, detail))

    scenes = [
        {
            "id": "l01",
            "start": 0.0,
            "end": 7.0,
            "voice_start": 0.55,
            "voice_end": 6.2,
            "text": "人工智能正在改变视频创作，从写稿到配音都能自动完成。",
        },
        {
            "id": "l02",
            "start": 7.0,
            "end": 13.5,
            "voice_start": 7.55,
            "voice_end": 12.7,
            "text": "但自动化不等于放弃检查，成片仍然需要逐句回读。",
        },
        {
            "id": "l03",
            "start": 13.5,
            "end": 19.9,
            "voice_start": 14.05,
            "voice_end": 19.0,
            "text": "只有时间轴对得上，字幕和旁白才不会是两张皮。",
        },
    ]
    good_segs = [
        {
            "start": 0.37,
            "end": 5.19,
            "text": "人工智能正在改变视频创作,从写稿到配音都能自动完成。",
        },
        {
            "start": 7.58,
            "end": 11.82,
            "text": "但自动化不等于放弃检查,成片仍然需要逐句回读。",
        },
        {
            "start": 14.17,
            "end": 18.13,
            "text": "只有时间轴对得上,字幕和旁白才不会是两张皮。",
        },
    ]

    def c_norm():
        # 用 whisper tiny 的真实输出（实测 .team/asr_fixture 那次）："視頻創作/從寫到配音/自動"
        a = _norm_text("人工智能正在改变視頻創作,从寫到配音都能自動完成。")
        b = _norm_text("人工智能正在改变视频创作，从写到配音都能自动完成")
        if a != b:
            return False, "归一化不一致：%r vs %r" % (a, b)
        if _norm_text("2024年 第3季度") != _norm_text("二零二四年第三季度"):
            return False, "数字写法归一不一致：%r vs %r" % (
                _norm_text("2024年 第3季度"),
                _norm_text("二零二四年第三季度"),
            )
        if _norm_text("，。！  ——") != "":
            return False, "纯标点没归零：%r" % _norm_text("，。！  ——")
        if _norm_text("一起") != "一起":
            return False, "单个'一'被误当数字：%r" % _norm_text("一起")
        return True, "繁简/标点/数字/单字数字都正确"

    def c_recall():
        if abs(_char_recall("你好世界", "你好世界") - 1.0) > 1e-9:
            return False, "完全一致应为 1.0"
        if abs(_char_recall("你好世界", "你好") - 0.5) > 1e-9:
            return False, "丢掉一半应为 0.5，实际 %r" % _char_recall("你好世界", "你好")
        if abs(_char_recall("是的是的", "是的") - 0.5) > 1e-9:
            return False, "多重集重复字应算 0.5，实际 %r" % _char_recall(
                "是的是的", "是的"
            )
        if abs(_char_recall("", "x") - 1.0) > 1e-9:
            return False, "空参考应为 1.0（由调用方判 skipped）"
        return True, "1.0 / 0.5 / 重复字 / 空参考都对"

    def c_assign():
        # 一个横跨 l01 尾与 l02 头的片段：按语音窗口比例裁切，两段都该拿到字
        segs = [{"start": 6.0, "end": 8.0, "text": "自动完成但自动化"}]
        got = _assign_text(scenes, segs)
        if not got[0]["text"] or not got[1]["text"]:
            return False, "跨句片段没有分给两段：%r / %r" % (
                got[0]["text"],
                got[1]["text"],
            )
        # 语音窗口：l01 到 6.2（重叠 0.2 s）、l02 从 7.55 开始（重叠 0.45 s），
        # 8 个字按 0.2:0.45 分 -> 2 个字给 l01、6 个字给 l02
        if got[0]["text"] != "自动" or got[1]["text"] != "完成但自动化":
            return (
                False,
                "跨句裁切比例不对：%r / %r（预期 '自动' / '完成但自动化'）"
                % (got[0]["text"], got[1]["text"]),
            )
        if got[0]["first_t"] is None or got[1]["first_t"] is None:
            return False, "跨句片段的 first_t 没算出来"
        return True, "跨句片段切成 %r / %r" % (got[0]["text"], got[1]["text"])

    def c_pass():
        j = _judge(scenes, good_segs)
        if j["gate"]["status"] != "pass":
            return False, "完美召回却判 %s：%s" % (j["gate"]["status"], j["errors"])
        if j["recall"] < 0.99:
            return False, "召回 %.3f 偏低" % j["recall"]
        if j["missing_lines"]:
            return False, "误报缺句：%s" % (j["missing_lines"],)
        if any("首字" in w for w in j["warnings"]):
            return False, "正常偏移被误报：%s" % j["warnings"]
        return True, "召回 %.3f，pass，无缺句/偏移告警" % j["recall"]

    def c_warn():
        # 少数几个字的写法差异 -> 召回落在 [warn, pass) 区间（pass 线刻意设成 1.0）
        segs = [
            {
                "start": 0.37,
                "end": 5.19,
                "text": "人工智能正在改变视频创作,从写稿到配音都能自动",
            },
            {
                "start": 7.58,
                "end": 11.82,
                "text": "但自动化不等于放弃检查,成片仍然需要逐句回读。",
            },
            {
                "start": 14.17,
                "end": 18.13,
                "text": "只有时间轴对得上,字幕和旁白才不会是两张皮。",
            },
        ]
        j = _judge(scenes, segs, recall_pass=1.0, recall_warn=0.80)
        if j["gate"]["status"] != "pass" or not any(
            "低于 pass 线" in w for w in j["warnings"]
        ):
            return False, "介于两线之间应 pass + warning，实际 %s / %s" % (
                j["gate"]["status"],
                j["warnings"],
            )
        return True, "召回 %.3f -> pass + %d 条 warning" % (
            j["recall"],
            len(j["warnings"]),
        )

    def c_fail():
        segs = [
            good_segs[0],
            {"start": 7.58, "end": 11.82, "text": "但自动化不等于放弃检查"},
        ]
        j = _judge(scenes, segs)
        if j["gate"]["status"] != "fail":
            return False, "丢一句应 fail，实际 %s（recall=%.3f）" % (
                j["gate"]["status"],
                j["recall"],
            )
        if "l03" not in j["missing_lines"]:
            return False, "丢句没进 missing_lines：%s" % (j["missing_lines"],)
        if not any("低于 fail 线" in e for e in j["errors"]):
            return False, "fail 没写 errors：%s" % (j["errors"],)
        return True, "丢一句 -> fail（recall %.3f），缺句 %s" % (
            j["recall"],
            j["missing_lines"],
        )

    def c_offset():
        # 「真的晚了」要被算成偏移并告警。构造要点（1.12 起 offset 判据分三档，用例必须
        # 明确自己落在哪一档，否则测的只是"某条分支碰巧走到"）：
        #   ① 片段**覆盖**期望开口时刻（7.40 <= voice_start 7.55 < 11.82），不触发"不可判"；
        #   ② 片段起点不早于期望 - 阈值，不触发"跨句伪偏移"；
        #   ③ 有词级时间戳（first_word_t=8.20），first_t 就是真实晚开时刻。
        # 旧写法只把整段后移 0.6 s 而不给词级时间戳：那时判据只有一条，现在会被
        # 正确归到"边界不可信（不可判）"——所以这里改用**可判**的构造，另开一条用例守不可判。
        segs = [dict(s) for s in good_segs]
        segs[1] = {
            "start": 7.40,
            "end": 11.82,
            "first_word_t": 8.20,
            "text": "但自动化不等于放弃检查,成片仍然需要逐句回读。",
        }
        j = _judge(scenes, segs)
        dt = j["offsets"].get("l02")
        if dt is None or abs(dt - 0.65) > 0.12:
            return False, "偏移算错：%r（预期约 +0.65）" % (dt,)
        if not any("l02" in w and "偏" in w for w in j["warnings"]):
            return False, "偏移超阈没告警：%s" % (j["warnings"],)
        if j["gate"]["status"] != "pass":
            return False, "偏移只该 warning，不该改判定（%s）" % j["gate"]["status"]
        return True, "l02 偏移 %+.2fs 记 warning，判定仍 pass" % dt

    def c_offset_uncertain():
        """ASR 边界不可信时：不算偏移、但必须**说出来**（不能静默当成 0）。

        两个真实踩过的坑合在一条用例里守：
          ① 片段起点晚于期望开口时刻、且期望时刻没有任何片段覆盖 —— 这时
             「first_t - 期望」既可能是真晚开，也可能只是 ASR 把弱起判成静音；
          ② 这种情况 seg_start 可能是 None（没有词级时间戳），旧代码
             `round(float(seg_start), 3)` 直接 TypeError，整条门禁变成"跑了就崩"；
          ③ 结论写进 offset_ref 后又被"来源"字段无条件覆盖，warnings 里只剩
             一句"建议试听"之前的线索全没了。
        """
        segs = [dict(s) for s in good_segs]
        segs[1] = {
            "start": 8.18,
            "end": 12.42,  # l02 整体晚 0.6 s：期望 7.55 无覆盖
            "text": "但自动化不等于放弃检查,成片仍然需要逐句回读。",
        }
        j = _judge(scenes, segs)
        if "l02" in j["offsets"]:
            return False, "边界不可信的段落仍被算成偏移：%r" % (j["offsets"]["l02"],)
        if "l02" not in j["offset_uncertain"]:
            return False, "没记进 offset_uncertain（结论丢了）：%r" % (
                j["offset_uncertain"],
            )
        if j["offset_uncertain"]["l02"].get("seg_start") is not None:
            return False, "无词级时间戳时 seg_start 应为 None，实际 %r" % (
                j["offset_uncertain"]["l02"].get("seg_start"),
            )
        if "不可判" not in j["offset_ref"].get("l02", ""):
            return False, "offset_ref 被来源字段覆盖（结论丢失）：%r" % (
                j["offset_ref"],
            )
        if not any("无法自动判定" in w and "l02" in w for w in j["warnings"]):
            return False, "不可判没告警：%s" % (j["warnings"],)
        if j["gate"]["status"] != "pass":
            return False, "不可判不该改判定（%s）" % j["gate"]["status"]
        return True, "l02 记不可判 + warning 需人工试听，判定仍 pass"

    def c_no_film():
        rep = report(os.path.join(tmp, "nope.mp4"), scenes, log=None)
        if rep["gate"]["status"] != "skipped":
            return False, "成片不存在应 skipped，实际 %s" % rep["gate"]["status"]
        if not rep["errors"]:
            return False, "skipped 却没写 errors"
        if not any("没有执行" in w for w in rep["warnings"]):
            return False, "skipped 没给'没查不等于通过'的 warning"
        return True, "成片不存在 -> skipped + errors + warning"

    def c_no_engine():
        """两个引擎都不可用时 skipped，且不能冒充 pass。"""
        old = (_FW_IMPORT, _FW_IMPORT_ERR)
        try:
            globals()["_FW_IMPORT"] = False
            globals()["_FW_IMPORT_ERR"] = "selftest 注入：import 失败"
            mp4 = os.path.join(tmp, "with_audio.mp4")
            if not _make_tiny_mp4(mp4):
                return None, "没有 ffmpeg，造不出测试 mp4"
            rep = report(
                mp4,
                scenes,
                engine="faster-whisper",
                log=None,
                whisper_model=os.path.join(tmp, "no_model.bin"),
            )
            if rep["gate"]["status"] != "skipped":
                return False, "引擎不可用应 skipped，实际 %s" % rep["gate"]["status"]
            if not rep["errors"]:
                return False, "引擎不可用没写 errors"
            if rep["judged"] is not False:
                return False, "skipped 时 judged 应为 False（没查不算查过）"
            return True, "引擎不可用 -> skipped（不冒充 pass），原因已写明"
        finally:
            globals()["_FW_IMPORT"], globals()["_FW_IMPORT_ERR"] = old

    def c_no_audio():
        mp4 = os.path.join(tmp, "no_audio.mp4")
        if not _make_tiny_mp4(mp4, with_audio=False):
            return None, "没有 ffmpeg，造不出无音轨 mp4"
        # 不传 asr_json：必须走真实引擎分支，验证"无音轨 -> skipped"
        rep = report(mp4, scenes, log=None)
        if rep["gate"]["status"] != "skipped":
            return False, "无音轨应 skipped，实际 %s" % rep["gate"]["status"]
        if not any("音轨" in e for e in rep["errors"]):
            return False, "无音轨原因没写进 errors：%s" % (rep["errors"],)
        return True, "无音轨 -> skipped + 说明"

    def c_replay():
        mp4 = os.path.join(tmp, "audio_only.mp4")
        if not _make_tiny_mp4(mp4):
            return None, "没有 ffmpeg，造不出测试 mp4"
        outj = os.path.join(tmp, "asr.json")
        rep = report(
            mp4, scenes, asr_json={"segments": good_segs}, out_json=outj, log=None
        )
        if rep["gate"]["status"] != "pass" or rep["engine"] != "replay":
            return False, "离线回放判分错：%s / %s" % (
                rep["gate"]["status"],
                rep["engine"],
            )
        if not (os.path.exists(outj) and os.path.getsize(outj) > 200):
            return False, "报告 JSON 没写出来"
        data = json.load(open(outj, encoding="utf-8"))
        need = {
            "judged",
            "engine",
            "recall",
            "per_line",
            "missing_lines",
            "offsets",
            "gate",
            "warnings",
            "errors",
        }
        lack = need - set(data)
        if lack:
            return False, "报告缺字段：%s" % sorted(lack)
        if data["gate"]["status"] not in ("pass", "fail", "skipped"):
            return False, "gate.status 非法：%r" % data["gate"]["status"]
        return True, "离线回放 pass，JSON 字段齐全（%d 字节）" % os.path.getsize(outj)

    def c_cli():
        mp4 = os.path.join(tmp, "cli.mp4")
        if not _make_tiny_mp4(mp4):
            return None, "没有 ffmpeg，造不出测试 mp4"
        tlj = os.path.join(tmp, "timeline.json")
        json.dump(
            {"total": 19.9, "scenes": scenes},
            open(tlj, "w", encoding="utf-8"),
            ensure_ascii=False,
        )
        asrj = os.path.join(tmp, "replay.json")
        json.dump(
            {"segments": good_segs},
            open(asrj, "w", encoding="utf-8"),
            ensure_ascii=False,
        )
        outj = os.path.join(tmp, "cli_asr.json")
        p = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "qaasr.py"),
                mp4,
                "--timeline",
                tlj,
                "--asr-json",
                asrj,
                "--out",
                outj,
            ],
            cwd=HERE,
            capture_output=True,
            timeout=180,
        )
        out = (p.stdout or b"").decode("utf-8", "replace")
        if p.returncode != 0:
            return False, "pass 时应 exit 0，实际 %d；%s" % (
                p.returncode,
                out[-300:] + (p.stderr or b"").decode("utf-8", "replace")[-200:],
            )
        if "字符召回" not in out:
            return False, "CLI 输出缺少中文摘要：%r" % out[:200]
        if not os.path.exists(outj):
            return False, "CLI 没写出 --out JSON"
        badj = os.path.join(tmp, "bad.json")
        json.dump(
            {"segments": good_segs[:1]},
            open(badj, "w", encoding="utf-8"),
            ensure_ascii=False,
        )
        p2 = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "qaasr.py"),
                mp4,
                "--timeline",
                tlj,
                "--asr-json",
                badj,
            ],
            cwd=HERE,
            capture_output=True,
            timeout=180,
        )
        if p2.returncode != 1:
            return False, "fail 时应 exit 1，实际 %d" % p2.returncode
        # 用法级错误（成片不存在）-> exit 2
        p3 = subprocess.run(
            [
                sys.executable,
                os.path.join(HERE, "qaasr.py"),
                os.path.join(tmp, "missing.mp4"),
                "--timeline",
                tlj,
                "--asr-json",
                asrj,
            ],
            cwd=HERE,
            capture_output=True,
            timeout=180,
        )
        if p3.returncode != 2:
            return False, "成片不存在应 exit 2，实际 %d" % p3.returncode
        return True, "pass -> 0；丢句 -> 1；用法错误（成片缺失）-> 2"

    def c_import():
        p = subprocess.run(
            [sys.executable, "-c", "import qaasr"],
            cwd=HERE,
            capture_output=True,
            timeout=120,
        )
        if p.returncode != 0:
            return False, "import 失败：%s" % (
                (p.stderr or b"").decode("utf-8", "replace")[-300:]
            )
        if (p.stdout or b"").strip():
            return False, "import 有打印：%r" % (p.stdout[:200],)
        return True, "干净退出，无打印"

    try:
        print("== qaasr.py selftest（不联网、不加载 ASR 模型）")
        check("文本归一化（繁简/标点/数字）", c_norm)
        check("字符级多重集召回", c_recall)
        check("跨句片段按语音窗口裁切", c_assign)
        check("完美召回 -> pass", c_pass)
        check("介于两线之间 -> pass + warning", c_warn)
        check("丢一句 -> fail", c_fail)
        check("偏移超阈 -> warning 不改判定", c_offset)
        check("ASR 边界不可判 -> warning 需人工试听", c_offset_uncertain)
        check("成片不存在 -> skipped", c_no_film)
        check("引擎不可用 -> skipped（不冒充 pass）", c_no_engine)
        check("无音轨 -> skipped", c_no_audio)
        check("离线回放 + 报告 JSON 字段", c_replay)
        check("CLI 端到端（退出码）", c_cli)
        check("import 无副作用", c_import)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _n, st, _d in results if st == "PASS")
    nfail = sum(1 for _n, st, _d in results if st == "FAIL")
    nskip = sum(1 for _n, st, _d in results if st == "SKIP")
    elapsed = time.perf_counter() - t_start
    print(
        "== 结果：%d PASS / %d FAIL / %d SKIP，耗时 %.1f s"
        % (npass, nfail, nskip, elapsed)
    )
    if nfail:
        return 1
    if nskip:
        print(
            "   [WARN] 有 %d 项因环境缺件跳过（%s），跳过的项不算通过"
            % (nskip, "、".join(n for n, st, _d in results if st == "SKIP"))
        )
    return 0


# ---------------------------------------------------------------- CLI
def _utf8_stdout() -> None:
    """被管道/文件重定向时切 UTF-8（与 render.py / qa_frames.py 同一口径）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="qaasr.py",
        description="ASR 回读校验：把成片音轨转回文字，与 timeline/剧本做字符级召回与偏移检查",
    )
    ap.add_argument("film", nargs="?", help="成片路径（有音轨即可，视频轨不参与）")
    ap.add_argument(
        "--timeline",
        default=None,
        help="work/timeline.json（scenes[].id/start/end，可选 voice_start/voice_end/text）",
    )
    ap.add_argument(
        "--script", default=None, help="film.json（给了就用 lines[].text 当原稿）"
    )
    ap.add_argument(
        "--model", default="small", help="faster-whisper 模型（默认 small）"
    )
    ap.add_argument("--lang", default="zh", help="ASR 语言（默认 zh）")
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径")
    ap.add_argument(
        "--recall-pass",
        type=float,
        default=QA_RECALL_PASS,
        help="pass 线（默认 %.2f，与 render.py 的 G12 门禁同一口径）" % QA_RECALL_PASS,
    )
    ap.add_argument(
        "--recall-warn",
        type=float,
        default=QA_RECALL_WARN,
        help="fail 线（默认 %.2f，与 render.py 的 G12 门禁同一口径）" % QA_RECALL_WARN,
    )
    ap.add_argument(
        "--offset-warn",
        type=float,
        default=OFFSET_WARN_S,
        help="首字偏移告警阈值秒（默认 %.2f）" % OFFSET_WARN_S,
    )
    ap.add_argument(
        "--limit-seconds",
        type=float,
        default=None,
        help="只对前 N 秒做 ASR（自测提速用，timeline 会同步裁剪）",
    )
    ap.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "faster-whisper", "ffmpeg"],
        help="ASR 引擎（默认 auto）",
    )
    ap.add_argument(
        "--asr-json",
        default=None,
        help="已有 ASR 结果 JSON（离线回放，不加载模型/不抽音频）",
    )
    ap.add_argument(
        "--whisper-model", default=None, help="ffmpeg whisper 滤镜用的 ggml 模型路径"
    )
    ap.add_argument(
        "--vad", action="store_true", help="faster-whisper 开 VAD（会多要一个模型）"
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="skipped 也算失败（与 render.py 的 --strict-gates 同义）",
    )
    ap.add_argument(
        "--selftest", action="store_true", help="跑内置自检（不联网，≤60 s）"
    )
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.film or not a.timeline:
        ap.error("需要成片路径与 --timeline（或用 --selftest）")
    _utf8_stdout()
    # 用法级错误单独判（exit 2）：路径打错和"环境缺件导致没查"不是一回事。
    # 后者由 report() 记 skipped + errors，默认 exit 0，--strict 才升级成失败，
    # 与 render.py 的 --strict-gates 语义保持同一口径。
    if not os.path.exists(str(a.film)):
        print("[FAIL] 成片不存在：%s" % a.film, file=sys.stderr)
        return 2
    if not os.path.exists(str(a.timeline)):
        print("[FAIL] timeline 不存在：%s" % a.timeline, file=sys.stderr)
        return 2
    try:
        rep = report(
            a.film,
            a.timeline,
            a.script,
            model_size=a.model,
            lang=a.lang,
            out_json=a.out,
            recall_pass=a.recall_pass,
            recall_warn=a.recall_warn,
            offset_warn=a.offset_warn,
            limit_seconds=a.limit_seconds,
            engine=a.engine,
            asr_json=a.asr_json,
            whisper_model=a.whisper_model,
            vad=a.vad,
        )
    except Exception as e:  # noqa: BLE001
        print("[FAIL] ASR 回读失败：%s" % e, file=sys.stderr)
        return 1
    status = (rep.get("gate") or {}).get("status")
    if status == "fail":
        return 1
    if status == "skipped":
        if a.strict:
            print(
                "   [FAIL] --strict：ASR 回读本次没有执行（skipped != 通过）",
                file=sys.stderr,
            )
            return 1
        print(
            "   [SKIP] ASR 回读没有执行（skipped != 通过；加 --strict 可让它失败）",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
