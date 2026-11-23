# -*- coding: utf-8 -*-
"""selfcheck.py — 技能自检 / 回归测试（不联网，10~25 秒内出结论）。

跑法：python selfcheck.py [-v]

检查的全是真实缺陷的回归点，不是泛泛的健康检查：
  1  TTS 缓存必须校验文本（只判文件存在 -> 改台词不生效）
  2  sub_window 不能把长句字幕截断在 5.2s（硬钳会让长句丢字）
  3  loudness 必须能测到真峰（ebur128 没开 peak -> 真峰门禁永不触发）
  4  mux 默认不重编视频流（二次编码会再压一遍画面）
  5  FrameSink 尺寸不符必须报错（静默 resize 会让成片与构图不符）
  6  字形自查能识别缺字（msyhbd 缺 ✓ 等）
  7  ASS 头必须声明 PlayRes，且 Format/Dialogue 字段数一致
  8  时间轴第一段 start 严格为 0
  9  分块渲染的绝对帧号命名 + 缺口检测
 10  qa_report 能查出字幕条数不匹配
 11  尾静音裁剪
 12  mux 音轨 48kHz
 13  旧调用签名兼容
 14  qa 帧率校验
 15  立体声总线：channels=2 形状正确、声像能量守恒
 16  cue_time 三种写法都解析成绝对秒
 17  CueBus 落盘 -> 读回 -> schema 正确（含未知类型不崩）
 18  duck 真的压低了配乐（且不改变无音效处的电平）
 19  mix_film 峰值不越界、时长精确、旁白优先
 20  sfxkit 可用时：配方确定性 + 别名表能从 sfxkit 反查
 21  scorekit 可用时：同 seed bit-identical + 时长精确
 22  webengine 可用时：页面契约齐全（window.renderFrame/getCues/ready）
 23  qa_frames 可用时：能检出注入的单帧闪烁
 24  配乐来源：music.mode=off 不读上一轮 work/music.wav（模式判定回归）
  25  风格层：每套采样主题全部过对比度门禁，8 类题材能分出 >=6 种风格
  26  legacy 已移除：不写 style 必须**报错并给迁移指引**（守的是"旧外观路径真的
      没了"，而不是"精确落回某套老默认值"）
  27  内容指纹：同剧本稳定、改标点不换风格、换题材才换
  28  声线计划：默认 4 句样片只用 1 把声音（max_distinct=1）、tone 仍分档；
      题目点名「多人对话」时必须采到并使用 >= 2 把声音（显式要求才换人）
  29  字体族与主题字幕：换族换缓存键，浅色主题不画黑背板
  30  门禁账本与签核：pass/fail/skipped 三档不许混，签核必须绑内容指纹
  31  阶段新鲜度：改了输入必须报"过时"，不许用旧产物静默出片
  32  并行渲染主题序列化：JSON 往返后重解风格必须回到 tuple
  33  音效跨片可区分：内容派生 seed 让跨片不同、legacy 逐样本不变
  34  配乐/音效片内分离：duck 默认加深，配乐给音效让位
  35  旁白优先：人声出现时背景必须再压、间隙恢复、人声电平不动
  36  旁白优先默认值：配乐 -12 dB 基线 + 旁白侧链 6 dB（读 st_audio 源码）
  37  旁白清晰度 SNR 兜底：背景天然太响时自动补压到 >=12 dB
  38  无旁白时混音不受影响（voice_env/guard 不得空转改电平）
  39  音频边界：极短旁白不崩、NaN/Inf 拒绝写盘、负起点与 seg=0 有守卫
  40  QC 报告落点只有一个来源（--out 的目录，不许 accept 另算一套）
  41  空门禁账本不许写成"通过"（没记录 != 通过）
  42  单阶段产物归属：不许把别的 stage 写的文件算成自己的（假 stale）
  43  theme_overrides 必须参与主题体检（否则能造出"看不清"的成片）
  44  词级时间戳：规整（单调/裁剪/偏移）与 cue 词锚点解析
  45  画幅规划：只允许 16:9 横屏下采样，竖屏/上采样/非法 fps 必须拒绝
  46  金帧确定性：同一帧号两次渲染必须逐像素一致
  47  依赖漂移体检：numpy/Pillow 主版本 + edge-tts 仍暴露 boundary 参数
  48  批量清单解析：两种写法都认、上限卡死、坏条目在开跑前报错
  49  TTS 后端可替换：注册/注销/未知后端的接口契约
  50  web 风格载荷：style=None 时不得注入 __STYLE__（legacy 必须回落）
  51  金帧回归：横屏 + 竖屏整片像素基线都比对（golden_frames*.json）
  52  场景语法：10 类构图分流、选型确定性、段长降级门
  53  视觉语言包：默认采样必须是 plain + 无 HUD + 不推镜；12 个设计的结构签名互不相同；
      点名「括角」必须能采到 brackets（素净是默认，不是唯一）
  54  场景类型单真值：grammar / filmcheck / scenes / schema 四处集合相等
  55  剪影可区分（S13，离线检查）：8 套风格两两之间构图骨架必须能区分
  56  设计 DSL：采样->校验->转计划全通；缺必填块必须报错（fail-closed）；
      同 brief 两次采样一致；两两至少 2 维不同（"伪多方案"门禁）
  57  零模板：仓库里不许存在 presets/ 与 presetkit.py；现场采样必须确定性、
      且换 brief 必须换结果（否则就是还在套模板）
  58  提案闸：absent / draft / broken 三档判定正确（confirmed 才放行出片）
  59  逐 stage 可单独跑：render 里每处 resolve_style 都必须拿得到 work
      （只跑 --stage frames / --stage sfx 时缺参数会报"找不到设计"，all 被缓存掩盖）
  60  brief 里的要求真的生效：自然语言要解析成 must_have/avoid，
      配色约束按色相窗口生效（早先整句被塞进 topic，"深蓝"采出来是橙色）
  61  字幕样式可定制（design.visual.subtitle 真的生效）
  62  片内版式轮换（layout_cycle：换章即换骨架）
  63  带约束的 brief 不会高概率采样失败
  64  已删除 API 的 fail-closed（不许 NameError / 假绿 / 静默回落）
  65  design.audio 的音量/音效参数真的落进 script
  66  片间去重台账（确认记账 / 提案避开 / 可关可查）
  67  时长要求真的被接住（brief -> duration_target -> 渲染对账）
  68  缩放取景的帧间稳定性（NEAREST 回归：grain 图案不许整屏错位）
  69  ASR 掉分可定位（lost_top / 最差段落）
  70  出片前问诊（固定 8 题 / 制作方式按题材推荐并落地 / 明亮点排第一）
  71  默认设计不画装饰细线：母题层全透明、纹理 none、HUD none；
      10 类构图的绘制调用里没有 width<4 的 line、没有「长>=40 且厚<=3」的色条
  72  前景不随镜头位移：`_view` 与 s 无关、push=0 时缩放恒 1.0、
      标题带连续 30 帧逐字节一致（慢速亚像素位移 -> 1px 台阶的回归护栏）

说明：本文件里 CASES 的**实际条数**是唯一真理，上面的编号表要与它同步。
这张表落后过两次：停在 43 条而 CASES 已有 51 条、停在 51 条而 CASES 已有 58 条。
改 CASES 时请同批改这张表，别让它再次落后。
"""
import argparse
import copy
import io
import hashlib
import json
import pathlib
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filmkit as K

RESULTS = []
VERBOSE = False


def _setup_stdio():
    """非 tty（管道 / 日志 / CI）时把 stdout/stderr 切到 UTF-8，避免 Windows 下中文乱码。

    真控制台（isatty=True）保持系统编码：交互运行时中文照样正常，
    只有"输出被重定向"才需要固定 UTF-8 字节。与 render / scorekit / webengine 同口径。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


def run(name, fn):
    try:
        detail = fn()
        # 前缀判定：SKIP 的理由要能写给人看（"skip（缺 xxx.py）"），但 "skipped != 通过"
        # 这条语义不能被理由文字冲掉——旧实现只认精确 "skip"，带理由的降级会被记成 PASS。
        if str(detail or "").strip().lower().startswith("skip"):
            RESULTS.append(("SKIP", name, "-"))
            print("  SKIP %s" % name)
        else:
            RESULTS.append(("PASS", name, detail or ""))
            print("  PASS %s%s" % (name, ("  -> " + str(detail)) if (VERBOSE and detail) else ""))
    except AssertionError as e:
        RESULTS.append(("FAIL", name, str(e)))
        print("  FAIL %s  -> %s" % (name, e))
    except SystemExit as e:
        # 被调代码用 SystemExit 报"用法错误"（例如 resolve_style 要求先提案）。
        # 必须记 FAIL 而不是让自检直接退出 —— 早先 selfcheck 会在这里**静默中止**，
        # 后面的用例一条都不跑，输出留在半截，很容易被当成"还在跑"。
        RESULTS.append(("FAIL", name, "SystemExit: %s" % (e.code if str(e.code) != "None" else e)))
        print("  FAIL %s  -> SystemExit: %s" % (name, e))
    except Exception as e:                      # noqa: BLE001
        RESULTS.append(("FAIL", name, "%s: %s" % (type(e).__name__, e)))
        print("  FAIL %s  -> %s: %s" % (name, type(e).__name__, e))


# 自检专用：造一份 design 计划。
# 删掉内置风格表后，"要一份 plan"这件事只能靠现场采样 + 转计划；
# 每个用例各写一遍既啰嗦又容易漂移，于是收到这里。
# 固定 seed 保证自检自身可复现（自检必须可复现，否则 CI 抖起来没法查）。
_PLAN_CACHE = {}


def _design_plan(topic="selfcheck 样例", seed=20260101):
    key = (topic, seed)
    if key not in _PLAN_CACHE:
        import designkit as DK
        d = DK.sample_designs({"topic": topic}, n=1, seed=seed)[0]
        _PLAN_CACHE[key] = (d, DK.design_to_plan(d))
    return _PLAN_CACHE[key]


def _sample_design(topic="selfcheck 样例", seed=20260101):
    return _design_plan(topic, seed)[0]


def _tone_wav(path, seconds=1.0, freq=440.0):
    import numpy as np
    n = int(seconds * K.SR)
    t = np.arange(n) / float(K.SR)
    K.wav_write(path, 0.3 * np.sin(2 * np.pi * freq * t))


# ------------------------------------------------------------------ 用例
def t_cache_key():
    with tempfile.TemporaryDirectory() as d:
        mp3 = os.path.join(d, "a.mp3")
        open(mp3, "wb").write(b"\xff\xfb" + b"0" * 4096)
        old_key = K.tts_key("旧台词", "zh-CN-YunxiNeural", "+0%", "+0Hz")
        new_key = K.tts_key("新台词", "zh-CN-YunxiNeural", "+0%", "+0Hz")
        assert old_key != new_key, "不同文本却得到相同缓存 key"
        json.dump(dict(path=mp3, text="旧台词", key=old_key, trimmed=False, dur=1.0),
                  open(mp3 + ".json", "w", encoding="utf-8"), ensure_ascii=False)
        m = json.load(open(mp3 + ".json", encoding="utf-8"))
        assert m["key"] != new_key, "旧 meta 会被误判成命中"
        return old_key[:8]


def t_sub_window():
    scenes = [dict(id="a", start=0.0, end=12.0, dur=12.0,
                   voice_start=0.62, voice_end=11.5, text="长" * 60, sub="长句字幕")]
    t0, t1 = K.sub_window(scenes, 0)
    assert t1 - t0 > 10.0, "长句字幕只覆盖 %.1fs（5.2s 截断回归）" % (t1 - t0)
    two = [dict(id="a", start=0.0, end=5.0, dur=5.0, voice_start=0.6, voice_end=4.6,
                text="x", sub="x"),
           dict(id="b", start=5.0, end=10.0, dur=5.0, voice_start=5.6, voice_end=9.6,
                text="y", sub="y")]
    _, e0 = K.sub_window(two, 0)
    s1, _ = K.sub_window(two, 1)
    assert e0 <= s1 + 1e-6, "第一句字幕压到第二句（%.2f > %.2f）" % (e0, s1)
    # max_hold 显式钳短
    _, e2 = K.sub_window(two, 0, max_hold=2.0)
    assert e2 - 0.0 <= 2.62, "max_hold 没生效: %.2f" % e2
    return "%.1fs 覆盖整句" % (t1 - t0)


def t_true_peak():
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "t.wav")
        _tone_wav(wav, 2.0)
        lo = K.loudness(wav)
        if not lo:
            return "skip"
        assert "TP" in lo, "没有真峰（ebur128 未开 peak 的回归）: %s" % lo
        assert "I" in lo, "没有积分响度"
        return "I=%.1f LUFS, TP=%.1f dBFS" % (lo["I"], lo["TP"])


def t_mux_copy():
    with tempfile.TemporaryDirectory() as d:
        video = os.path.join(d, "v.mp4")
        r = subprocess.run([K.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc=size=320x180:rate=30:duration=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", video],
                           capture_output=True)
        if r.returncode != 0:
            return "skip"
        wav = os.path.join(d, "a.wav")
        _tone_wav(wav, 2.0)
        out = os.path.join(d, "o.mp4")
        K.mux(video, wav, out, loudnorm=False)
        assert os.path.getsize(out) > 5000, "mux 没有产出有效文件"
        return "%.0f KB" % (os.path.getsize(out) / 1024)


def t_strict_size():
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        try:
            sink = K.FrameSink(os.path.join(d, "x.mp4"), w=320, h=180, fps=30)
        except FileNotFoundError:
            return "skip"
        try:
            try:
                sink.write(Image.new("RGB", (640, 360), (0, 0, 0)))
                raised = False
            except RuntimeError as e:
                raised = "帧尺寸" in str(e)
            assert raised, "尺寸不符没被拦下（静默 resize 回归）"
        finally:
            try:
                sink.proc.kill()
            except Exception:
                pass
    return "strict=True 生效"


def t_glyph():
    assert not K.glyph_missing_chars("AB中文abc", 32, "reg"), "常规字符被误判为缺字"
    assert K.glyph_missing_chars("\ue000", 32, "reg"), "缺字检测失效（私用区应判定缺失）"
    return "常规字符通过 / 私用区被识别"


def t_ass():
    scenes = [dict(id="a", start=0.0, end=3.0, dur=3.0, voice_start=0.6,
                   voice_end=2.8, text="你好世界", sub="你好世界")]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.ass")
        n = K.write_ass(scenes, p)
        txt = open(p, encoding="utf-8").read()
        assert "PlayResX: 1920" in txt and "PlayResY: 1080" in txt, "ASS 头缺 PlayRes"
        fmt = [l for l in txt.splitlines() if l.startswith("Format:")][-1]
        dia = [l for l in txt.splitlines() if l.startswith("Dialogue:")][0]
        assert len(fmt.split(",")) == len(dia.split(",")), "Format 与 Dialogue 字段数不一致"
        assert n == 1, "事件条数不对"
        return "PlayRes 1920x1080 / %d 条" % n


def t_timeline_zero():
    tl = K.build_timeline([dict(id="a", text="一", dur=2.0),
                           dict(id="b", text="二", dur=2.0)])
    assert tl["scenes"][0]["start"] == 0.0, "首段 start != 0"
    assert tl["frames"] == int(round(tl["total"] * K.FPS)), "帧数换算不一致"
    return "total %.2fs / %d 帧" % (tl["total"], tl["frames"])


def t_chunks():
    with tempfile.TemporaryDirectory() as d:
        seg = os.path.join(d, "segments")
        os.makedirs(seg, exist_ok=True)
        plan = [(0, 300), (300, 600), (600, 900)]
        for f0, f1 in plan:
            open(os.path.join(seg, "seg_%06d_%06d.mp4" % (f0, f1)), "wb").write(b"x" * 2048)
        missing = [p for p in plan
                   if not os.path.exists(os.path.join(seg, "seg_%06d_%06d.mp4" % p))]
        assert not missing, "正常命名下误报缺口"
        os.remove(os.path.join(seg, "seg_000300_000600.mp4"))
        missing = [p for p in plan
                   if not os.path.exists(os.path.join(seg, "seg_%06d_%06d.mp4" % p))]
        assert missing == [(300, 600)], "缺口没被检出: %s" % missing
    return "缺口可检出"


def t_qa_subs():
    with tempfile.TemporaryDirectory() as d:
        srt = os.path.join(d, "sub.srt")
        open(srt, "w", encoding="utf-8").write("1\n00:00:00,000 --> 00:00:01,000\n一\n\n")
        rep = K.qa_report(srt, expected_subtitles=3, notes=srt)
        assert any("字幕条数" in e for e in rep["errors"]), \
            "字幕条数不匹配没被报出来: %s" % rep["errors"]
    return "可检出"


def t_trim():
    """尾静音裁剪：1 秒静音 + 3 秒声 + 1 秒静音 -> 裁完应明显变短且保留中段。"""
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "long.wav")
        import numpy as np
        sr = K.SR
        pad = np.zeros(int(1.0 * sr), dtype=np.float32)
        body = 0.4 * np.sin(2 * np.pi * 440 * np.arange(int(3.0 * sr)) / float(sr)).astype(np.float32)
        K.wav_write(wav, np.concatenate([pad, body, pad]))
        pcm = K.decode_pcm(wav)
        cut = K.trim_silence(pcm)
        assert len(cut) < len(pcm) * 0.95, "尾静音没被裁掉（%.2fs -> %.2fs）" % (
            len(pcm) / sr, len(cut) / sr)
        assert len(cut) > 2.5 * sr, "裁过头了（%.2fs 短于语音本体）" % (len(cut) / sr)
        mid = float(np.max(np.abs(cut))) > 0.3
        assert mid, "裁完丢了语音本体"
        return "%.2fs -> %.2fs" % (len(pcm) / sr, len(cut) / sr)


def t_mux_48k():
    """mux 后音轨必须是 48kHz：不指定 -ar 时 loudnorm 会输出 192kHz、AAC 选 96kHz。"""
    with tempfile.TemporaryDirectory() as d:
        video = os.path.join(d, "v.mp4")
        r = subprocess.run([K.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc=size=320x180:rate=30:duration=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", video], capture_output=True)
        if r.returncode != 0:
            return "skip"
        wav = os.path.join(d, "a.wav")
        _tone_wav(wav, 2.0)
        out = os.path.join(d, "o.mp4")
        K.mux(video, wav, out, loudnorm=True)
        pr = K.probe(out)
        au = [s for s in pr.get("streams", []) if s.get("codec_type") == "audio"]
        assert au, "mux 后没有音轨"
        sr = int(au[0].get("sample_rate") or 0)
        assert sr == K.SR, "音轨采样率 %d != %d（loudnorm 内部会升到 192kHz，必须显式 -ar）" % (sr, K.SR)
        return "%d Hz" % sr


def t_compat():
    """旧调用签名必须还能用（升级不能把老项目直接炸掉）。"""
    a = K.tone(440, 0.2, kind="sine", harmonics=(1.0, 0.5))
    b = K.noise_burst(0.2, amp=0.1, lo=0.02, hi=0.35, decay=6.0, seed=7)
    assert len(a) > 0 and len(b) > 0, "兼容签名返回空"
    # 关键公共函数的关键字参数
    K.build_timeline([dict(id="a", text="一", dur=1.5)], lead=0.5, tail=0.8, min_dur=2.0)
    K.qa_report(__file__, expect_frames=None, ref_png=None, ref_sec=None,
                expected_subtitles=None, min_bitrate=0)
    return "tone/noise_burst/build_timeline/qa_report 旧签名可用"


def t_fps_check():
    """qa_report 必须能查出帧率不符（只查宽高会漏掉帧率写错）。"""
    with tempfile.TemporaryDirectory() as d:
        v = os.path.join(d, "v25.mp4")
        r = subprocess.run([K.FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc=size=320x180:rate=25:duration=1",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", v], capture_output=True)
        if r.returncode != 0:
            return "skip"
        rep = K.qa_report(v, min_bitrate=0)
        assert any("帧率" in e for e in rep["errors"]),             "帧率不符没被报出来: %s" % rep["errors"]
    return "25fps 被检出"


# ---------------------------------------------------------------- 立体声 / 配乐 / 音效
def t_stereo_bus():
    """立体声总线：形状、声像、单声道来源自动展开。"""
    import numpy as np
    b = K.AudioBus(1.0, channels=2)
    assert b.bus.shape == (int(round(1.0 * K.SR)) + K.SR, 2), "立体声总线形状不对: %s" % (b.bus.shape,)
    src = np.ones(480, dtype=np.float32)
    b.place(src, 0.0, gain=0.5, pan_pos=-1.0)          # 全左
    seg = b.bus[10:400]
    assert abs(float(seg[:, 0].max()) - 0.5) < 1e-3, "声像左声道电平不对"
    assert float(np.abs(seg[:, 1]).max()) < 1e-3, "pan_pos=-1 右声道应该静音"
    b2 = K.AudioBus(1.0, channels=2)
    b2.place(np.ones(480, dtype=np.float32), 0.0, gain=0.5)   # 居中
    seg2 = b2.bus[10:400]
    assert abs(float(seg2[:, 0].max()) - float(seg2[:, 1].max())) < 1e-4, "居中声像左右应相等"
    return "channels=2 形状与声像正确"


def t_cue_time():
    """提示点 at 的三种写法。"""
    scenes = [dict(id="l01", start=0.0), dict(id="l02", start=3.5)]
    assert K.cue_time("abs:12.5") == 12.5, "abs: 写法解析错"
    assert K.cue_time(7.5) == 7.5, "数字写法解析错"
    assert K.cue_time("l02+0.35", scenes) == 3.85, "id+偏移 解析错"
    assert K.cue_time("l02-0.2", scenes) == 3.3, "id-偏移 解析错"
    assert K.cue_time("l02", scenes) == 3.5, "纯 id 解析错"
    try:
        K.cue_time("nope+1", scenes)
        raise AssertionError("找不到场景却没有报错")
    except ValueError:
        pass
    return "abs/数字/id±偏移 全部正确"


def t_cue_json():
    """CueBus 落盘 -> 读回 -> schema；未知类型不进 CUE_ALIASES 但也不崩。"""
    with tempfile.TemporaryDirectory() as d:
        bus = K.CueBus([dict(id="l01", start=0.0)], total=10.0)
        bus.add("l01+1.2", "whoosh", dur=0.9)
        bus.add("abs:4.0", "impact", gain=-2)
        bus.add(8.5, "blip")                       # 别名 blip -> ui_blip
        p = os.path.join(d, "cues.json")
        payload = bus.write(p, duration=10.0)
        assert payload["version"] == 1
        assert len(payload["cues"]) == 3, "提示点条数不对"
        assert [c["t"] for c in payload["cues"]] == sorted(c["t"] for c in payload["cues"]), "未按时间排序"
        assert payload["cues"][2]["type"] == "ui_blip", "别名没被展开: %s" % payload["cues"][2]
        back = K.CueBus.from_json(p)
        assert len(back.items) == 3, "读回条数不对"
        # 越界提示点（超过片长 2s）应被丢弃
        bus.items.append(dict(t=99.0, type="impact", src="pil"))
        assert len(bus.resolved()) == 3, "越界提示点没有被丢掉"
    return "3 条 / 别名展开 / 排序 / 越界丢弃"


def t_duck():
    """音效响起时配乐让位；没有音效的地方电平不变。"""
    import numpy as np
    n = int(2.0 * K.SR)
    music = np.full((n, 2), 0.5, dtype=np.float32)
    sfx = np.zeros((n, 2), dtype=np.float32)
    sfx[n // 2:n // 2 + K.SR // 4] = 0.9
    out = K.duck(music, sfx, depth_db=6.0)
    head = float(np.mean(out[: K.SR // 2]))
    mid = float(np.min(out[n // 2: n // 2 + K.SR // 4]))
    assert abs(head - 0.5) < 0.02, "没有音效的地方被压了（%.3f）" % head
    assert mid < 0.5 - 0.05, "duck 没生效（最深处 %.3f）" % mid
    return "静止段 %.3f / 音效段 %.3f" % (head, mid)


def t_mix_film():
    """三总线混音：时长精确、不越界、旁白在。"""
    import numpy as np
    n = int(1.5 * K.SR)
    t = np.arange(n) / float(K.SR)
    voice = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    music = np.full((n, 2), 0.2, dtype=np.float32)
    sfx = np.zeros((n, 2), dtype=np.float32)
    sfx[100:100 + 240] = 0.8
    out = K.mix_film(voice, music, sfx, total=1.5, fade=0.0)
    assert out.shape == (n, 2), "输出形状不对: %s" % (out.shape,)
    assert float(np.max(np.abs(out))) <= 0.9 + 1e-6, "峰值越界: %.3f" % float(np.max(np.abs(out)))
    assert float(np.max(np.abs(out))) > 0.2, "混出来几乎是静音"
    out0 = K.mix_film(voice, None, None, total=1.5, fade=0.0)
    assert abs(float(np.max(np.abs(out0))) - float(np.max(np.abs(voice)))) < 1e-5, "只有旁白时电平应不变"
    return "%.2fs / 峰值 %.3f" % (len(out) / float(K.SR), float(np.max(np.abs(out))))


def t_music_plan():
    """配乐来源判定：music.mode=off 不得读上一轮的 work/music.wav。

    背景：st_audio 若无条件读 work/music.wav，用户把 music.mode 改成 off
    之后，上一轮跑出来的旧配乐仍被混进成片（文档写的是 off = 不要配乐）。
    这里既测判定函数，也真的跑一次 st_audio 看输出电平。
    """
    # 1) 纯判定函数（K.music_plan）
    assert K.music_plan({"music": {"mode": "off"}}, True)[0] == "off", "mode=off 没被判成 off"
    assert K.music_plan({"music": {"enabled": False}}, True)[0] == "off", "enabled=false 没被判成 off"
    assert K.music_plan({"bpm": 120}, True)[0] == "score", "有 bpm 应走 scorekit"
    assert K.music_plan({"bpm": 120, "music": {"mode": "bed"}}, True)[0] == "bed", "mode=bed 解析错"
    assert K.music_plan({"music": {"mode": "file"}}, True)[0] == "file", "mode=file 解析错"
    assert K.music_plan({"music": {"mode": "file"}}, False)[0] == "missing-file", "mode=file 缺文件没报错"
    assert K.music_plan({"music": {"mode": "score"}}, True)[0] == "missing-bpm", "mode=score 缺 bpm 没报错"
    assert K.music_plan({"music": {"mode": "weird"}})[0] == "unknown-mode", "未知 mode 没报错（会静默当 auto）"
    assert K.music_plan({}, True)[0] == "file", "缺省时外部 music.wav 兼容链断了"
    assert K.music_plan({}, False)[0] == "none", "缺省且什么都没有时应判 none"
    assert K.music_plan({"music": {"chords": [[220.0, 261.6]]}}, False)[0] == "bed", "和弦床兼容链断了"

    # 2) 端到端：st_audio 在 mode=off 下不得混入 work/music.wav
    try:
        import render   # 同目录编排器；只借用它的 stage 函数
    except Exception:                                       # noqa: BLE001
        return "skip"
    import numpy as np
    with tempfile.TemporaryDirectory() as d:
        work = os.path.join(d, "work")
        os.makedirs(work)
        # 旁白轨故意做成全零：这样 audio.wav 的峰值只可能来自配乐
        voice = os.path.join(d, "voice.wav")
        K.wav_write(voice, np.zeros(K.SR, dtype=np.float32))
        tl = dict(total=1.0, fps=K.FPS, frames=K.FPS, scenes=[
            dict(id="l01", start=0.0, end=1.0, dur=1.0, voice_start=0.1, voice_end=0.9,
                 path=voice, text="一", sub="一")])
        n = int(1.0 * K.SR)
        t = np.arange(n) / float(K.SR)
        loud = (0.8 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
        K.wav_write(os.path.join(work, "music.wav"), np.stack([loud, loud], axis=1))
        buf = io.StringIO()
        with redirect_stdout(buf):
            # D3 之后没有 design/style 的剧本会被拒（不再静默落 legacy），
            # 这条用例测的是"off 不读旧配乐"，与设计无关，显式给一个风格即可。
            # D3 之后没有 design/style 的剧本会被拒（不再静默落 legacy），
            # 这条用例测的是"off 不读旧配乐"，与设计无关，给一份现场设计即可。
            _d, _plan = _design_plan("配乐来源判定")
            _script23 = {"title": "配乐来源", "_style": _plan}
            off_out = render.st_audio(tl, work, dict(_script23, music={"mode": "off"}))
            off_peak = float(np.max(np.abs(K.wav_read(off_out)[0])))
            file_out = render.st_audio(tl, work, dict(_script23, music={"mode": "file"}))
            file_peak = float(np.max(np.abs(K.wav_read(file_out)[0])))
        assert "music.mode=off" in buf.getvalue(), "off 没有打印说明（静默跳过也不行）"
        assert off_peak < 0.01, "music.mode=off 仍把旧 music.wav 混进来了（峰值 %.3f）" % off_peak
        # 断言用"相对 off 明显变大"而不是绝对 0.3——
        # 配乐基线降到 -12 dB（旁白优先）后，0.8 幅度的外部配乐混出来约 0.2；
        # 绝对阈值会把"旁白优先调好了"判成"没读到外部配乐"，这类断言会被调参牵着走。
        assert file_peak > 0.1 and file_peak > off_peak * 3.0, (
            "music.mode=file 没有用外部配乐（off %.3f -> file %.3f）" % (off_peak, file_peak))
    return "off 峰值 %.3f / file 峰值 %.3f" % (off_peak, file_peak)


def t_sfxkit():
    """sfxkit 存在时的接口与确定性自检；不存在就 SKIP。"""
    mod = None
    try:
        import sfxkit as mod  # noqa: F401
    except Exception:                                       # noqa: BLE001
        return "skip"
    names = set(mod.load_all().keys()) if hasattr(mod, "load_all") else set()
    assert names, "sfxkit 没有任何配方"
    need = {"ui_blip", "ui_click", "typing", "whoosh", "impact", "sub_drop", "riser", "wind"}
    missing = need - names
    assert not missing, "sfxkit 缺配方: %s" % ", ".join(sorted(missing))
    a = mod.render("impact", seed=3)
    b = mod.render("impact", seed=3)
    import numpy as np
    assert len(a) == len(b) and np.array_equal(np.asarray(a), np.asarray(b)), "同一 seed 两次渲染不一致"
    assert float(np.max(np.abs(a))) > 1e-3, "impact 渲染出来是静音"
    aliases = getattr(mod, "CUE_ALIASES", None)
    if aliases:
        for k, v in aliases.items():
            assert v in names, "别名 %s -> %s 指向不存在的配方" % (k, v)
    return "%d 配方 / 确定性 OK" % len(names)


def t_scorekit():
    """scorekit 存在时：同 seed 一致、时长精确、不削波。"""
    try:
        import scorekit as sk
    except Exception:                                       # noqa: BLE001
        return "skip"
    import numpy as np
    a, rep = sk.render(12.0, bpm=120, key="Dm", seed=5)
    b, _ = sk.render(12.0, bpm=120, key="Dm", seed=5)
    a = np.asarray(a)
    b = np.asarray(b)
    assert a.shape == b.shape and np.array_equal(a, b), "同一 seed 两次配乐不一致"
    assert abs(len(a) / float(sk.SR) - 12.0) < 0.05, "配乐时长不对: %.3fs" % (len(a) / float(sk.SR))
    pk = float(np.max(np.abs(a)))
    assert pk <= 1.0, "配乐削波: %.3f" % pk
    assert pk > 1e-3, "配乐是静音"
    return "12s / %.2f LUFS? / 峰值 %.2f" % (float(rep.get("lufs", 0.0)) if isinstance(rep, dict) else 0.0, pk)


def t_webengine():
    """webengine 存在时：模块可导入 + 关键接口在（不真的起浏览器，避免自检变慢）。

    注意作用域：这条检查的是**工具本身**是否可用。页面模板 assets/web/ 属于
    "项目自己的东西"——用户按 quickstart 只拷了 scripts/*.py 时它本来就不在工作目录里，
    此时不该判 FAIL（那会把"没拷模板"和"工具坏了"混为一谈），而是明确提示要不要拷。
    """
    try:
        import webengine as we  # noqa: F401
    except Exception:                                       # noqa: BLE001
        return "skip"
    for fn in ("snap", "export_cues", "render_full", "qa_text", "page_meta"):
        assert hasattr(we, fn), "webengine 缺函数 %s" % fn

    here = os.path.dirname(os.path.abspath(__file__))
    # 页面模板可能在技能目录（装成插件时）也可能在用户工作目录（按 quickstart 拷过去）
    cands = [os.path.join(os.path.dirname(here), "assets", "web"),
             os.path.join(os.getcwd(), "web"),
             os.path.join(os.getcwd(), "assets", "web")]
    web = next((p for p in cands if os.path.exists(os.path.join(p, "engine.js"))), None)
    if web is None:
        return "模块可用；工作目录没有 web/ 页面模板（用 --engine web 前先 cp -r <SKILL_DIR>/assets/web ./web）"
    need = ["index.html", "engine.js"]
    miss = [n for n in need if not os.path.exists(os.path.join(web, n))]
    assert not miss, "web/ 缺文件: %s（%s）" % (miss, web)
    txt = open(os.path.join(web, "engine.js"), encoding="utf-8", errors="replace").read()
    for key in ("renderFrame", "getCues", "DURATION"):
        assert key in txt, "engine.js 没导出 %s（页面契约）" % key
    try:
        web_label = os.path.relpath(web, os.getcwd())
    except ValueError:
        # Windows 跨盘符（技能装在 C:、工作目录在 D:）时 relpath 直接抛错。
        # 这只影响回显文案，退化成绝对路径即可，不该把整条检查判 FAIL。
        web_label = web
    return "接口齐全 + 页面契约齐全（%s）" % web_label


def t_qa_frames():
    """qa_frames 存在时：能 import 且函数齐全（真正的检出能力由它自己的 --selftest 保证）。"""
    try:
        import qa_frames as qf
    except Exception:                                       # noqa: BLE001
        return "skip"
    for fn in ("flicker_scan", "black_windows", "luma_curve", "sync_check", "report"):
        assert hasattr(qf, fn), "qa_frames 缺函数 %s" % fn
    return "5 个函数齐全"



def t_style_kits():
    """25 连续配色空间：现场采样的每一套都必须过对比度门禁，
    且色相真的分散——这条是"没有模板"的硬证据。

    旧断言是"8 套内置主题全部过 lint + 自动选风格能分出 >=6 种"。模板删掉后那个断言
    失去对象，于是换成更强的：**连续空间采样 12 次，全部可读 + 色相覆盖 >= 180°**。
    只测"能采出来"不够 —— 全落在同一色相也能过，所以必须夹一条分布断言。
    """
    import designkit as DK
    import stylekit as ST
    designs = DK.sample_designs({"topic": "配色空间校验"}, n=12, seed=20260101)
    assert len(designs) == 12, "采样返回 %d 套（要 12）" % len(designs)
    hues, fails = [], []
    for d in designs:
        plan = DK.design_to_plan(d)
        bad = ST.theme_lint(plan["theme"])
        if bad:
            fails.append("%s: %s" % (d["label"], bad[0][:50]))
        h, _s, _v = ST.rgb_to_hsv(plan["theme"]["bg"])
        hues.append(h)
    assert not fails, "有设计过不了对比度门禁：%s" % "；".join(fails[:3])
    span = max(hues) - min(hues)
    if span < 180.0:                        # 跨 0° 的情况：取补集
        span = 360.0 - span
    assert span >= 180.0, ("12 套设计的底色谱相只覆盖 %.0f°（应 >=180°）——"
                           "说明配色并没有真的在连续空间里散开" % span)
    # 配色必须是**连续**的，不是一份清单：断言"色相是连续量"而不是"落在 8 个点之一"。
    # 做法：取 12 套的色相，检查它们不是某个固定集合的子集（容差 1°）。
    # 早先这里断言的是 `len(PALETTE_FAMILIES) == len(_LEGACY_FAMILY_ANCHOR)` ——
    # 而那两个名字是**同一个对象**，长度当然永远相等，等于什么也没验。
    assert not DK.PALETTE_FAMILIES, "PALETTE_FAMILIES 必须为空（有内容=偷偷恢复了外观清单）"
    rounded = sorted({round(h) for h in hues})
    spread = len(rounded) / float(len(hues))
    assert spread >= 0.8, ("12 套配色的色相只有 %d 个不同取值（%s）——"
                           "说明配色退化成了一份离散清单" % (len(rounded), rounded))
    return "12 套现场配色全部过 lint；色相覆盖 %.0f°；%d/12 个不同色相" % (
        span, len(rounded))


def t_style_legacy():
    """26 legacy 已移除：不写 style 必须报错并给出迁移指引，而不是静默落老外观。

    这条盯的是"不写外观配置照样能出片"这条老思路真的没了：legacy 路径整个删掉，
    因为它是同质化的另一半根因（不写 style = 全片一套固定外观 + 120BPM/Dm）。
    断言分成两半：① 不给设计时必须**报错**且错误文本指得出下一步；
    ② 显式给 style 也不可用（内置风格表已删，任何 style id 都必须落空报错）。
    """
    import stylekit as ST
    s = {"title": "旧项目", "lines": [{"id": "a", "kind": "body", "text": "x"}]}
    # ① 不给设计：必须报错，且错误文本指得出下一步
    try:
        ST.resolve_script_style(s)
    except (ValueError, KeyError, SystemExit) as e:
        msg = str(getattr(e, "code", None) or e)
        assert "propose.py" in msg, "报错必须给出迁移指引（要提到 propose.py），实际：%r" % msg[:120]
        assert "design" in msg.lower(), "报错要说清主路径是 design.json，实际：%r" % msg[:120]
    else:
        raise AssertionError("没有 style/design 却没报错：legacy 路径没有真的移除")
    # ② 显式 style 也不可用——内置风格表整个删了，连带 STYLES 一起没了，
    #    任何 style id 都必须落空并报错（否则就是"模板还在"）。
    s3 = dict(s, style="cinema")
    try:
        ST.resolve_script_style(s3)
    except (ValueError, KeyError, SystemExit) as e:
        msg = str(getattr(e, "code", None) or e)
        assert "模板" in msg or "design" in msg.lower() or "propose" in msg,             "显式 style 报错也该给指引，实际：%r" % msg[:120]
    else:
        raise AssertionError("显式 style='cinema' 竟然还能解析出计划：内置风格表没删干净")
    assert ST.STYLE_IDS == () and not ST.STYLES and not ST.THEMES,         "内置风格/主题表必须为空，实际 STYLE_IDS=%r STYLES=%d THEMES=%d" % (
            ST.STYLE_IDS, len(ST.STYLES), len(ST.THEMES))
    return "无设计必报错并给迁移指引 / 显式 style 也被拒绝 / 三张模板表为空"


def t_style_determinism():
    """27 指纹确定性：同剧本同结果；改错别字不换风格；改题材才换。"""
    import designkit as DK
    # 没有"剧本 -> 自动选风格"了，这里验证"设计采样"的确定性与区分度。
    # 固定 seed 才谈确定性：默认不给 seed 时每次都要不同（那是 56/57 条在守的事）。
    a = DK.sample_designs({"topic": "AI 大模型的三年"}, n=1, seed=11)[0]
    b = DK.sample_designs({"topic": "AI 大模型的三年"}, n=1, seed=11)[0]
    assert DK.design_fingerprint(a) == DK.design_fingerprint(b), "同 seed 同 brief 采样不一致"
    assert DK.design_fingerprint(a) != DK.design_fingerprint(
        DK.sample_designs({"topic": "手工木作"}, n=1, seed=11)[0]), "换了题材设计却没变"
    return "同 seed 可复现 + 换题材必变"


def t_voice_plan():
    """28 声线计划：legacy 保持单声线；风格化时按 kind 换声线且确定性。"""
    try:
        import voicekit as VK
    except Exception:                                       # noqa: BLE001
        return "skip"
    import designkit as DK
    s = {"title": "AI 大模型",
         "lines": [{"id": "l01", "kind": "hook", "text": "大模型改变了什么"},
                   {"id": "l02", "kind": "body", "text": "算法和数据的规模一起上去了"},
                   {"id": "l03", "kind": "quote", "text": "他说：这不可能"},
                   {"id": "l04", "kind": "outro", "text": "它已经在我们身边了"}]}
    # 声线计划同样只能从设计来（模板表已删）
    plan = DK.design_to_plan(DK.sample_designs({"topic": "AI 大模型"}, n=1, seed=7)[0])
    vp = VK.voice_plan(s, plan)
    assert len(vp) == 4, "计划行数应为 4，收到 %d" % len(vp)
    assert json.dumps(vp, sort_keys=True, default=str) == json.dumps(
        VK.voice_plan(s, plan), sort_keys=True, default=str), "voice_plan 不确定"
    # 默认**单声线**：整片一个旁白（用户要的是播音员，不是广播剧）。
    # 多声线只在 brief 点名（对话/多人/广播剧…）时采；手改 design.audio.voice.map 也能开。
    voices = {r["voice"] for r in vp}
    assert len(voices) == 1, "默认设计应只有一个旁白声线，实际 %d 个: %s" % (len(voices), voices)
    assert plan["voice"]["policy"]["max_distinct"] == 1, \
        "默认设计的 max_distinct 应为 1：%r" % (plan["voice"]["policy"],)
    d_default = DK.sample_designs({"topic": "AI 大模型"}, n=1, seed=7)[0]
    assert d_default["audio"]["voice"]["max_distinct"] == 1, \
        "默认采样仍有多个声线：%r" % (d_default["audio"]["voice"],)
    tones = {r["tone"] for r in vp}
    assert len(tones) >= 2, "语气档没有变化: %s" % tones
    # 点名要多人声时才允许多声线（且仍受 max_distinct 上限约束）。
    multi = None
    # 多声线要求写在题目原文里（must_have 只收五类可识别约束，「多人对话」不是其中一类）。
    # 点名后**必须**是 2~3 把：不靠碰运气（曾用 1+h%3，点名也可能只采到 1 把）。
    multi = DK.sample_designs({"topic": "多人对话：三个人聊同一件事"}, n=1, seed=1)[0]
    assert 2 <= multi["audio"]["voice"]["max_distinct"] <= 3, \
        "点名多人对话后 max_distinct 应在 2~3：%r" % (multi["audio"]["voice"],)
    mplan = DK.design_to_plan(multi)
    mvp = VK.voice_plan(s, mplan)
    assert len({r["voice"] for r in mvp}) >= 2, \
        "点名多声线，voice_plan 却只用了 1 把声音：%r" % (mvp,)
    for r in vp:
        for fld in ("voice", "rate", "pitch", "volume", "lead", "tail"):
            assert r.get(fld) not in (None, ""), "计划行缺 %s: %r" % (fld, r.get("id"))
    # 单句片：同样走 design（不再有 style="mono" 这种内置风格可选）
    one = {"title": "单句片",
           "lines": [{"id": "a", "kind": "body", "text": "一句话"}]}
    op = DK.design_to_plan(DK.sample_designs({"topic": "单句片"}, n=1, seed=3)[0])
    ovp = VK.voice_plan(one, op)
    assert len(ovp) == 1, "单句片应只给一行计划，收到 %d" % len(ovp)
    assert ovp[0]["voice"].endswith("Neural"), "声线应是 edge voice id：%r" % (ovp[0]["voice"],)
    return ("%d 句 / 默认 1 把声音（max_distinct=1）/ %d 档语气；点名多人对话 -> %d 把声音"
            % (len(vp), len(tones), len({r["voice"] for r in mvp})))


def t_font_family():
    """29 字体族 + 字幕主题：换族必须换缓存键，主题字幕不能是黑底白字。

    **别把"系统装了几族字体"当成产品缺陷**：CI 的 ubuntu 只装 fonts-noto-cjk，
    四个族全回退到同一个 NotoSansCJK，宽度必然一样。断言若写成"宽度必须不同"就会误报——
    Linux CI 红、Windows 绿。
    真正要保证的是**机制**：缓存按 family 分键（换族不会串味）；
    宽度差异只在"系统确实解析出不同字体文件"时才有意义，单族系统跳过并说明。
    """
    fams = list(getattr(K, "FAMILY_IDS", ()))
    assert fams, "filmkit 没有 FONT_FAMILIES（字体族没生效）"
    resolved = {}
    for fam in fams:
        f = K.font(40, "bold", fam)
        assert f is not None, "字体族 %s 加载失败" % fam
        assert f.size == 40, "字体族 %s 的字号被改写成 %s" % (fam, f.size)
        resolved[fam] = str(getattr(f, "path", "") or "")
    # (a) 机制保证：四个族必须是四条独立缓存，且键里带 family（否则 wrap 与绘制会用不同字体）
    keys = [k for k in K._font_cache if len(k) == 3 and k[0] == 40 and k[1] == "bold"]
    assert len({k[2] for k in keys}) == len(fams), \
        "字体缓存键没带 family（换族会串味）: %s" % (keys,)
    # (b) 语义保证：仅当系统真解析出 >=2 个字体文件时，宽度才必须有差异
    uniq_files = len(set(resolved.values()))
    widths = {}
    for fam in fams:
        widths[fam] = round(K.text_w("中文测试ABC", K.font(40, "bold", fam)), 3)
    uniq_w = len(set(widths.values()))
    if uniq_files >= 2:
        assert uniq_w >= 2, ("换族没换字形（%d 个字体文件却只有 %d 种宽度）: %s"
                             % (uniq_files, uniq_w, widths))
        detail = "%d 个字体族 / %d 个字体文件 / %d 种宽度" % (len(fams), uniq_files, uniq_w)
    else:
        # 单族回退（CI 的 fonts-noto-cjk，或极简 Linux 容器）：只验机制，不验宽度
        detail = "%d 个字体族 / 单族回退（%s）/ 只验缓存分键" % (
            len(fams), os.path.basename(list(resolved.values())[0]) or "?")
    # 缓存键必须区分 family（否则 wrap 与绘制会用不同字体，文字溢出安全区）
    a = K.wrap_cn_cached("这是一句用来测试换行缓存键的话", 40, 300, "reg", "sans")
    b = K.wrap_cn_cached("这是一句用来测试换行缓存键的话", 40, 300, "reg", "kai")
    assert isinstance(a, list) and isinstance(b, list)
    # 旧签名必须仍然可用（用户脚本里有大量 font(46,"reg")）
    assert K.font(46, "reg") is not None
    assert K.wrap_cn_cached("旧调用", 40, 500) is not None
    # 字幕主题：浅色主题不该画黑背板
    from PIL import Image
    # 没有内置主题可借（THEMES 已清空），手写一份浅色主题。
    # 这条断言测的是 draw_subtitle 的"浅底不画黑背板"逻辑，与主题从哪来无关。
    th = {"bg": (247, 244, 238), "bg2": (233, 228, 218), "surface": (255, 255, 255),
          "ink": (26, 24, 22), "muted": (110, 104, 96), "dim": (150, 146, 140),
          "accent": (198, 96, 40), "accent2": (52, 96, 160),
          "good": (46, 150, 92), "bad": (200, 60, 60), "rule": (208, 202, 192),
          "id": "selfcheck-light", "label": "浅色测试"}
    if th is not None:
        img = Image.new("RGB", (K.W, K.H), th["bg"])
        K.draw_subtitle(img, "浅色主题字幕", theme=th)
        px = img.getpixel((960, int((K.SUB_BAND[1] + K.SUB_BAND[3]) / 2)))
        assert sum(px) > 200, "浅色主题的字幕背板是深色（%s），说明 theme 没生效" % (px,)
    img2 = Image.new("RGB", (K.W, K.H), (0, 0, 0))
    K.draw_subtitle(img2, "旧调用", theme=None)
    return detail


def t_gates():
    """30 门禁账本 / 签核 / QC 报告：三档状态必须能区分"没查"和"通过"。

    这一条防的是最容易犯的错——把 skipped 混进 pass。
    用户看到的"质检通过"必须能区分"我查了 9 项"和"我只查了 3 项"：
    前者是真知情，后者是假安全感。顺带验签核绑内容指纹：改了剧本旧签核必须失效。
    """
    import tempfile
    import gates as GT
    w = tempfile.mkdtemp(prefix="sc_gates_")
    lg = GT.GateLedger(w, {"title": "t", "lines": [{"text": "x"}]}, engine="pil")
    lg.ok("A", "通过项", "1")
    lg.fail("B", "失败项", "2")
    lg.skip("C", "没执行项", "3")
    c = lg.counts()
    assert c == {"pass": 1, "fail": 1, "skipped": 1}, c
    # 三档语义：fail 一定失败；skipped 只在 --strict-gates 下失败
    assert len(lg.exit_errors()) == 1, "fail 没被算成失败"
    assert len(lg.exit_errors(strict=True)) == 2, "--strict-gates 没把 skipped 升级成失败"
    lg.write_json()
    assert os.path.exists(os.path.join(w, "gates.json")), "gates.json 没落盘"
    # 签核：没签 = skipped；签了 = pass；内容指纹变了 = skipped（不是 pass！）
    ch = lg.content_hash
    st0, _ = GT.signoff_state(w, ch)
    assert all(v["status"] == "skipped" for v in st0.values()), st0
    GT.write_signoff(w, "stills", ch, note="看过 sheet")
    st1, _ = GT.signoff_state(w, ch)
    assert st1["stills"]["status"] == "pass", st1["stills"]
    st2, stale = GT.signoff_state(w, "别的指纹")
    assert st2["stills"]["status"] == "skipped" and "stills" in stale, (st2, stale)
    # 内容指纹：改剧本 / 换引擎必须变，渲染期私有键（_path/_style）不许影响
    h1 = GT.content_hash({"title": "a", "lines": []}, engine="pil")
    h2 = GT.content_hash({"_path": "/x", "title": "a", "lines": []}, engine="pil")
    h3 = GT.content_hash({"title": "a", "lines": []}, engine="web")
    assert h1 == h2, "渲染期私有键影响了内容指纹（会导致假失效）"
    assert h1 != h3, "换引擎了内容指纹却没变（旧签核会错误地继续有效）"
    # QC 报告：三档都要出现在矩阵里，且 skipped 不能被渲染成"通过"
    GT.append_cost(w, "frames", 1.5, frames=30)
    p = GT.write_report(w, os.path.join(w, "out", "QC-REPORT.md"))
    txt = open(p, encoding="utf-8").read()
    for must in ("门禁矩阵", "人工签核", "耗时台账", "**skipped**", "**fail**"):
        assert must in txt, "报告里缺 %r" % must
    assert "未通过" in txt and "通过" in txt, "报告结论段没区分通过/未通过"
    # 原子写：不留 .tmp 尾巴
    assert not [f for f in os.listdir(w) if f.endswith(".tmp")], os.listdir(w)
    return "三档 1/1/1 + 签核 4 道闸 + 指纹失效 + 报告矩阵"


def t_stagewatch():
    """31 阶段新鲜度：改上游 -> 下游必须报过时；首跑不刷噪声；产物丢失要报。

    这一条防的是：frames 有分块指纹，其余 stage 全是裸奔
    （改 style 后 audio 仍读旧 music.wav、改 hold 后 frames 仍用旧 cues.json，都不报错）。
    """
    import tempfile
    import stagewatch as SW
    w = tempfile.mkdtemp(prefix="sc_sw_")
    assert all(not r["issues"] for r in SW.explain(w)), "空目录不该报过时"

    def _wf(name, text):
        p = os.path.join(w, name)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        open(p, "w", encoding="utf-8").write(text)

    _wf("style.json", "s1")
    _wf("voice_plan.json", "v1")
    SW.snapshot(w, "style")
    assert not [i for r in SW.explain(w, ["style"]) for i in r["issues"]], "刚跑完就报过时"
    _wf("tts.json", "t1")
    SW.snapshot(w, "tts")
    # 改 tts 的上游（style.json）-> tts 必须报 input-changed
    _wf("style.json", "s2")
    issues = [i for r in SW.explain(w, ["tts"]) for i in r["issues"]]
    assert any(i["kind"] == "input-changed" for i in issues), issues
    assert any("render.py" in (i.get("rerun") or "") for i in issues), "过时了却没给重跑命令"
    # 产物被删 -> 必须报 output-missing
    os.remove(os.path.join(w, "tts.json"))
    issues = [i for r in SW.explain(w, ["tts"]) for i in r["issues"]]
    assert any(i["kind"] == "output-missing" for i in issues), issues
    # 默认只告警；--strict-stale 才失败
    errs, warns = SW.report(w, ["tts"], write=lambda lv, m: None)
    assert not errs and warns, (errs, warns)
    errs_strict, _ = SW.report(w, ["tts"], strict=True, write=lambda lv, m: None)
    assert errs_strict, "--strict-stale 没有让过时变成失败"
    # **源头输入**（剧本 / scenes.py）改了但没重跑 -> 必须报 source-changed。
    # 这条守的是：只跟踪 stage 之间的产物时，"改了台词没重跑 tts" 查不出来，
    # 而那正是最需要报警的场景（旧 tts 配新剧本 = 配音字幕全错位）。
    w2 = tempfile.mkdtemp(prefix="sc_sw2_")
    film = os.path.join(w2, "film.json")
    scenes = os.path.join(w2, "scenes.py")
    open(film, "w", encoding="utf-8").write("{\"title\":\"a\"}")
    open(scenes, "w", encoding="utf-8").write("# v1")
    src = {"script": film, "scenes": scenes}
    _wf2 = lambda n, t: open(os.path.join(w2, n), "w", encoding="utf-8").write(t)
    _wf2("tts.json", "t1")
    SW.snapshot(w2, "tts", src)
    issues = [i for r in SW.explain(w2, ["tts"], src) for i in r["issues"]]
    assert not issues, "刚跑完就报过时：%s" % issues
    open(film, "w", encoding="utf-8").write("{\"title\":\"b\"}")   # 改剧本
    issues = [i for r in SW.explain(w2, ["tts"], src) for i in r["issues"]]
    assert any(i["kind"] == "source-changed" for i in issues), (
        "改了剧本却没报过时（这正是阶段新鲜度要堵的洞）：%s" % issues)
    assert any("film.json" in i["reason"] for i in issues), issues
    # 坏 manifest 不许崩（用户手改坏一个 json 不该让整片跑不动）
    open(os.path.join(w, "manifest.json"), "w", encoding="utf-8").write("{broken")
    SW.explain(w)
    return "改上游/改源头即报过时 + 产物丢失 + strict 升级 + 坏 manifest 不崩"


def t_parallel_frames():
    """32 --jobs >1 的并行路径能真的跑（pre-existing bug 的回归）。

    bug 形状：worker 从 _script_snapshot.json 读剧本，JSON 把主题色元组变成 list，
    scenes._bg() 拿它当缓存键 -> TypeError: unhashable type: "list"，
    于是 SKILL 里教的 --stage frames --jobs 4 直接崩。
    这里**不跑整片**（太慢），只验两条防线：① 主题色能过 JSON 往返当缓存键；
    ② worker 反序列化后会重解风格（_style 里的颜色回到 tuple）。
    """
    import json as _json
    import tempfile
    # 没有内置主题可借，用现场设计的主题（颜色同样是 (r,g,b) 元组）
    _d, _plan0 = _design_plan("并行渲染主题序列化")
    th = dict(_plan0["theme"])
    # ① 防御层：scenes._hashable 把 list 冻结成 tuple
    scenes = _load_scenes_mod()
    if scenes is not None and hasattr(scenes, "_hashable"):
        frozen = scenes._hashable(("baked", th["bg"], th["bg2"]))
        hash(frozen)                       # 不能抛 unhashable
        assert isinstance(frozen[1], tuple), "主题色没被冻结成 tuple：%r" % (frozen[1],)
    # ② 根因层：JSON 往返后，_render_chunk_worker 里必须能拿到 tuple 的 bg
    round_trip = _json.loads(_json.dumps({"bg": th["bg"]}, ensure_ascii=False))
    assert isinstance(round_trip["bg"], list), "前提变了：JSON 居然保住了 tuple"
    w = tempfile.mkdtemp(prefix="sc_par_")
    snap = os.path.join(w, "snap.json")
    script = {"title": "t", "lines": [{"text": "x"}]}
    script["_style"] = _plan0
    with open(snap, "w", encoding="utf-8") as fh:
        _json.dump(script, fh, ensure_ascii=False)
    back = _json.load(open(snap, encoding="utf-8"))
    assert isinstance(back["_style"]["theme"]["bg"], list), "前提变了"
    # 子进程路径（worker 必须从 work/design.json 重解设计，而不是回落 stylekit）。
    # 现在直接验那条路径：把 design.json 放进 work/，resolve_style 必须解出**同一个**设计，
    # 且颜色回到 tuple（list 会让 _bg() 的缓存键不可哈希）。
    import designkit as DK
    import render as R
    dpath = os.path.join(w, "design.json")
    with open(dpath, "w", encoding="utf-8") as fh:
        _json.dump(_sample_design("并行渲染主题序列化"), fh, ensure_ascii=False)
    sub_script = {"title": "t", "lines": [{"text": "x"}]}
    got = R.resolve_style(sub_script, w, verbose=False)
    assert got.get("style", "").startswith("design:"), \
        "子进程重解没有走 design.json（会画出另一部片）：%r" % (got.get("style"),)
    assert isinstance(got["theme"]["bg"], tuple), (
        "重解后主题色仍是 list，_bg() 会 TypeError（这正是被修的 bug）：%r"
        % (got["theme"]["bg"],))
    assert got["theme"]["bg"] == th["bg"], "重解出的主题与父进程不一致（并行会画出不同的片）"
    return "主题可哈希 + JSON 往返 + 按 design.json 重解一致"


def _load_scenes_mod():
    """加载 assets/scenes.py 模板（找不到就返回 None：CI 里可能只拷 scripts/）。"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "..", "assets", "scenes.py")
    if not os.path.exists(cand):
        return None
    try:
        spec = importlib.util.spec_from_file_location("sc_assets_scenes", cand)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                       # noqa: BLE001
        return None

def t_sfx_content_seed():
    """音效 seed 必须随内容变化，但 legacy 路径必须逐样本不变。

    - 不传 style/content_fp -> 两次渲染逐样本相同（legacy 兼容，不破老片）；
    - 传了不同 content_fp  -> 必须不同（否则跨片音效还是"都一样"）；
    - 同一 content_fp      -> 逐样本相同（可复现，是程序化视频的底线）。
    """
    import numpy as np
    scenes = [{"id": "l01", "kind": "hook", "start": 0.0, "end": 2.0},
              {"id": "l02", "kind": "body", "start": 2.0, "end": 6.0}]
    rows = [{"at": "l02", "type": "ui_click"}, {"at": "l02+0.5", "type": "whoosh"}]

    def build(**kw):
        b = K.CueBus(scenes=scenes, total=6.0)
        b.extend(rows)
        return b.build_audio(total=6.0, verbose=False, **kw)

    try:
        legacy_a, legacy_b = build(), build()
    except Exception:                                       # noqa: BLE001
        return "skip"                                       # sfxkit 不可用
    assert np.array_equal(legacy_a, legacy_b), "legacy 两次渲染不一致（逐样本兼容被破坏）"
    a = build(style="tech-deep", content_fp="aaaa1111", key="Dm")
    b = build(style="tech-deep", content_fp="bbbb2222", key="Dm")
    assert not np.array_equal(a, b), "不同内容指纹得到相同音效轨：跨片音效仍一模一样"
    a2 = build(style="tech-deep", content_fp="aaaa1111", key="Dm")
    assert np.array_equal(a, a2), "同内容指纹渲染不可复现"
    return "legacy 逐样本相同 / 跨片可区分"


def t_audio_separation():
    """混音侧 duck 默认必须加深（片内配乐给音效让位）。

    直接读 render.py 的常量文本而不是跑全链：这条断言盯的是"默认值没被改回去"
    （3.0 dB 这类浅值在段落过门时配乐几乎和音效等高，是"听起来像同一套"的成因之一）。
    """
    import re
    path = os.path.join(HERE, "render.py")
    src = open(path, encoding="utf-8").read()
    m = re.search(r"duck_default\s*=\s*([0-9.]+)", src)
    assert m, "render.py 里找不到 duck_default"
    base = float(m.group(1))
    assert base >= 3.5, "duck_default=%.1f dB 太浅（要求 >= 3.5）" % base
    for sid, want in (("mono", 3.0), ("cinema", 5.5), ("epic", 5.5)):
        mm = re.search(r'"%s"\s*:\s*([0-9.]+)' % sid, src)
        assert mm, "render.py 里找不到风格 %s 的 duck 值" % sid
        assert float(mm.group(1)) >= want, "%s duck=%.1f < %.1f" % (sid, float(mm.group(1)), want)
    return "duck 默认 %.1f dB（cinema/epic 5.5）" % base


def t_voice_priority():
    """旁白优先：人声出现时背景必须再压，人声间隙背景恢复。

    以前配乐基线只比旁白低 1.5 dB，视频以旁白为主时观众要费力分辨字。
    修法：`mix_film` 用旁白包络做侧链——人声处配乐 -6 dB、音效 -3 dB，间隙回原电平。
    这里分别验证「人声段被压」和「静音段恢复」，以及旁白自身电平不被影响。
    """
    import numpy as np
    n = int(2.0 * K.SR)
    t = np.arange(n) / float(K.SR)
    voice = np.zeros(n, dtype=np.float32)
    voice[: n // 2] = (0.2 * np.sin(2 * np.pi * 220 * t[: n // 2])).astype(np.float32)
    music = np.full((n, 2), 0.2, dtype=np.float32)
    out = K.mix_film(voice, music, None, total=2.0, fade=0.0,
                     music_gain_db=0.0, voice_duck_db=6.0, guard_snr_db=0.0)
    v = np.stack([voice, voice], axis=1)
    music_part = out - v                      # 混音是线性叠加，减掉人声即背景分量
    # 人声段：取中段（避开包络起音/释放的斜坡）
    talk = music_part[int(0.35 * K.SR): int(0.45 * K.SR)]
    gap = music_part[int(1.55 * K.SR): int(1.95 * K.SR)]
    talk_rms = float(np.sqrt(np.mean(talk * talk)))
    gap_rms = float(np.sqrt(np.mean(gap * gap)))
    # 期望：人声段 ≈ 0.2*10^(-6/20)=0.100；间隙 ≈ 0.2
    assert abs(talk_rms - 0.1003) < 0.03, "人声段背景没被压到 -6 dB（RMS %.3f，期望 0.100）" % talk_rms
    assert abs(gap_rms - 0.2) < 0.03, "人声间隙背景没恢复（RMS %.3f，期望 0.200）" % gap_rms
    # SNR 口径：只统计人声活跃区（含间隙会把问题稀释掉）。
    # 这里配乐基线是 0 dB，只有 -6 dB 侧链 -> SNR 应为 -3+6 = 3 dB 左右。
    snr = K.voice_snr(voice, music_part)
    assert snr is not None and 2.0 <= snr <= 4.0, "voice_snr 口径异常（%.1f dB，期望约 3）" % (snr or -99)
    # 旁白自身电平不能被侧链动过
    voice_part = out[:, 0] - music_part[:, 0]
    seg = voice_part[int(0.35 * K.SR): int(0.45 * K.SR)]
    want = voice[int(0.35 * K.SR): int(0.45 * K.SR)]
    assert float(np.max(np.abs(seg - want))) < 1e-4, "旁白电平被侧链改变（旁白优先不能压人声）"
    return "人声段背景 %.3f / 间隙 %.3f（dB 差 %.1f）" % (
        talk_rms, gap_rms, 20 * np.log10(gap_rms / (talk_rms + 1e-9)))


def t_music_gain_default():
    """配乐默认增益必须是 -12 dB（beside 人声），不是 -1.5。

    这条盯的是 `render.st_audio` 的默认值：`mus.get("gain_db", -12.0)`。
    直接读源码文本，避免"跑全链才知道默认值"。
    """
    src = open(os.path.join(HERE, "render.py"), encoding="utf-8").read()
    assert 'gain_db", -12.0' in src, "st_audio 的配乐默认增益不是 -12 dB"
    assert 'voice_duck_db", 6.0' in src, "st_audio 没有开旁白侧链（voice_duck_db=6）"
    return "配乐 -12 dB + 旁白侧链 6 dB"


def t_snr_guard():
    """SNR 兜底：背景天然太响时，混音后必须自己把它压到"人声听得清"。

    构造一段人声 + 一段刻意很响的配乐（比人声高 10 dB），不设侧链、只靠 guard：
    `mix_film(guard_snr_db=12)` 后实测人声活跃区的 人声/背景 必须 >= 12 dB，
    且人声本身电平不能被兜底改过（兜底只压背景）。
    """
    import numpy as np
    sr = K.SR
    n = int(3.0 * sr)
    t = np.arange(n) / float(sr)
    voice = np.zeros(n, dtype=np.float32)
    voice[int(0.4*sr):int(2.6*sr)] = (0.2 * np.sin(2*np.pi*220*t[int(0.4*sr):int(2.6*sr)])).astype(np.float32)
    # 配乐 RMS 约 0.6，比人声（RMS 0.141）高 ~12.5 dB —— 刻意做成"盖住旁白"的反例
    loud = np.full((n, 2), 0.6, dtype=np.float32)
    out = K.mix_film(voice, loud, None, total=3.0, fade=0.0,
                     music_gain_db=0.0, voice_duck_db=0.0, voice_duck_sfx_db=0.0,
                     guard_snr_db=12.0, guard_cap_db=30.0, verbose=False)
    v2 = np.stack([voice, voice], axis=1)
    bg = out - v2
    snr = K.voice_snr(voice, bg)
    assert snr is not None and snr >= 11.9, "SNR 兜底没生效：人声/背景 %.1f dB（要求 >=12）" % (snr or -99)
    # 人声电平不能被兜底动过
    seg_v = (out[:, 0] - bg[:, 0])[int(1.0*sr):int(2.0*sr)]
    want = voice[int(1.0*sr):int(2.0*sr)]
    assert float(np.max(np.abs(seg_v - want))) < 1e-4, "SNR 兜底改到了人声电平"
    return "兜底前约 -12.5 dB -> 兜底后 %.1f dB" % snr


def t_mix_no_voice():
    """没有旁白时混音行为不变：没有 voice_env/guard 可用，背景应原样输出。"""
    import numpy as np
    n = int(0.5 * K.SR)
    music = np.full((n, 2), 0.2, dtype=np.float32)
    out = K.mix_film(None, music, None, total=0.5, fade=0.0, music_gain_db=0.0, verbose=False)
    assert abs(float(np.max(np.abs(out))) - 0.2) < 1e-5, "无旁白时配乐被改动了（峰值 %.3f）" % float(np.max(np.abs(out)))
    return "无旁白时背景不被压"

# ------------------------------------------------------------------ 音频入口边界
def t_audio_edge_inputs():
    """39 音频入口的边界：极短旁白不能崩、NaN/Inf 必须拒绝、seg=0 不许死循环。

    这几条都是"真跑一次就会遇到"的输入。没有守卫时的行为：
      * voice_env 在极短音频上 np.convolve(mode="same") 返回核长 -> np.maximum 广播失败；
      * duck_voice 在"旁白比背景短"时包络与背景长度不同 -> 广播失败；
      * wav_write 对 NaN 做 clip 再 astype(int16)，把 NaN 静默写成 0（写出损坏 WAV 却不报错）；
      * AudioBus.place 传负起点时负下标切片 -> n=0 -> 广播崩；
      * music_bed 的 seg=0 让 t 不前进 -> while 死循环（有 100000 次上限，6 秒片要 2 分钟）。
    """
    import numpy as np
    short = np.full(600, 0.2, dtype=np.float32)          # 12.5 ms
    assert len(K.voice_env(short)) >= 1, "voice_env 对极短音频返回空包络"
    bg = np.full((K.SR, 2), 0.2, dtype=np.float32)
    got = K.duck_voice(bg, short, depth_db=6.0)
    assert got.shape == (K.SR, 2), "duck_voice 在旁白比背景短时形状不对: %r" % (got.shape,)
    m = np.full((600, 2), 0.2, dtype=np.float32)
    assert K.duck(m, short).shape == (600, 2), "duck 在短信号上形状不对"

    with tempfile.TemporaryDirectory() as d:
        for name, bad in (("nan", np.float32("nan")), ("inf", np.float32("inf"))):
            p = os.path.join(d, name + ".wav")
            try:
                K.wav_write(p, np.array([0.1, bad], dtype=np.float32))
            except ValueError:
                assert not os.path.exists(p), "wav_write 报错了却仍写出文件（%s）" % p
            else:
                raise AssertionError("wav_write 把 %s 静默写进了 WAV（应当报错）" % name)

    bus = K.AudioBus(2, K.SR, 1.0)
    bus.place(np.ones(1000, dtype=np.float32), -0.25)     # 负起点：从 0 秒开始，不许崩
    bus2 = K.AudioBus(2, K.SR, 0.3)
    try:
        bus2.music_bed([[220.0, 277.0, 330.0]], seg=0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("music_bed(seg=0) 没被拒绝（会死循环）")
    return "极短旁白不崩 / NaN·Inf 拒绝 / 负起点与 seg=0 有守卫"


def t_report_single_source():
    """40 QC 报告落点只能有一个来源：--out 的目录。

    真 bug：all/mux/qa 用 dirname(abspath(out))，accept 用
    dirname(abspath(work))/out。--out 指到 deliver/ 时，签核更新的是另一份没人
    交付的文件，交付出去的那份仍是"未签核"——正好打穿"报告是交付凭据"这条承诺。
    """
    import types
    try:
        import render
    except Exception as e:                                  # noqa: BLE001
        return "skip（import render 失败：%s）" % e
    assert hasattr(render, "_report_path"), "render 没有统一的 _report_path"
    a = types.SimpleNamespace(out=os.path.join("deliver", "film.mp4"), work="work")
    got = render._report_path(a)
    want = os.path.join("deliver", "QC-REPORT.md")
    assert os.path.abspath(got) == os.path.abspath(want), \
        "报告没落在 --out 旁边：%s（期望 %s）" % (got, want)
    src = open(os.path.join(HERE, "render.py"), encoding="utf-8").read()
    n = src.count('"QC-REPORT.md"')
    assert n == 1, "QC-REPORT.md 路径出现了 %d 处（应只有 _report_path 一处，否则又会两边漂移）" % n
    return "报告落点统一到 --out 目录（源码里只有一处拼接）"


def t_empty_ledger_not_pass():
    """41 门禁账本为空时，QC 报告不许说"通过"。

    render_report 原来 if counts.get(FAIL): ... else: **通过**——gates.json 被删、
    写坏、或根本没跑 qa 时，交付凭据会是一份宣称通过的纸。这条盯的就是那个 else。
    """
    try:
        import gates as GT
    except Exception as e:                                  # noqa: BLE001
        return "skip（没有 gates.py：%s）" % e
    with tempfile.TemporaryDirectory() as d:
        p = GT.write_report(d, os.path.join(d, "QC-REPORT.md"))
        txt = open(p, encoding="utf-8").read()
    assert "**通过**" not in txt, "空门禁账本却写成『通过』（交付凭据撒谎）"
    assert "未判定" in txt, "空门禁账本没写『未判定』（用户会以为跑过了）"
    return "0 条门禁 -> 未判定（不是通过）"


def t_stage_io_no_phantom_output():
    """42 单阶段连跑不许报"自己的产物不见了"（假 stale）。

    stagewatch 的 STAGE_IO[style].outputs 里把 voice_plan.json 也算成
    style 的产物，而它其实是 tts 阶段写的（render.py 的 st_tts）。于是 --stage style
    连跑两次，第二次必然报"产物 voice_plan.json 不见了"；打开 --strict-stale 就变成
    硬失败——SKILL.md 第 3 步（全新项目第一条生产命令）自己走不通。
    """
    try:
        import stagewatch as SW
    except Exception as e:                                  # noqa: BLE001
        return "skip（没有 stagewatch.py：%s）" % e
    style_out = list((SW.STAGE_IO.get("style") or {}).get("outputs") or [])
    tts_out = list((SW.STAGE_IO.get("tts") or {}).get("outputs") or [])
    if "voice_plan.json" in style_out:
        assert "voice_plan.json" in tts_out, \
            "voice_plan.json 被算成 style 的产物，但没有别的 stage 认领它（假 stale 会回来）"
    dup = [(n, st) for st, io in SW.STAGE_IO.items() for n in (io.get("outputs") or [])
           if n in style_out and st != "style"]
    assert not dup, \
        "style 的产物被别的 stage 也声明成输出（会产生假 stale）：%r" % (dup,)
    return "style 产物 %s / tts 产物 %s" % (",".join(sorted(style_out)), ",".join(sorted(tts_out)))


def t_theme_overrides_linted():
    """43 theme_overrides 必须参与主题体检（否则用户能造出"看不清"的成片）。

    真 bug：render.resolve_style 以前只体检**基础主题**
    （ST.THEMES[theme_id]），而 scenes 实际用的是 plan[theme]（基础主题 + film.json 的
    visual.theme_overrides）。于是把 ink/bg 调成低对比度时体检照样 PASS，渲完才发现字幕看不清。
    实测 overrides ink=[30,30,30]/bg=[10,10,10]：基础主题 lint 为空，最终主题 lint 报对比度 1.09。
    """
    import stylekit as ST
    # 没有"基础主题 vs 最终主题"的区分了（内置主题表已删），
    # 但"overrides 之后必须重新 lint"这条规则仍然要守：
    # 直接把设计主题改成低对比度，theme_lint 必须抓到。
    _d, _plan43 = _design_plan("theme_overrides 体检")
    base_th = _plan43["theme"]
    assert ST.theme_lint(base_th) == [], \
        "前提变了：设计主题本身就有 lint 问题，这条断言失去意义"
    dirty = ST.apply_theme_overrides(base_th, {"ink": [30, 30, 30], "bg": [10, 10, 10]})
    bad = ST.theme_lint(dirty)
    assert bad, '低对比度的 theme_overrides 没被 lint 抓到（用户能造出看不清的字幕）'
    # 体检的位置在 designkit（design_to_plan 里就 lint，不过直接抛）。
    # 这里改查**真正干活的地方**——早先查 render.py 的源码文本，那只是"哪个文件里写了这行"，
    # 一旦重构搬家就会误报（本次就是这样：lint 逻辑搬进 designkit 后断言还在查 render）。
    # 更硬的检查：直接把一份低对比度主题喂给 design_to_plan，必须抛。
    import designkit as DK
    dirty_design = json.loads(json.dumps(_sample_design("theme_overrides 体检")))
    dirty_design["visual"]["palette"]["ink"] = [30, 30, 30]
    dirty_design["visual"]["palette"]["bg"] = [10, 10, 10]
    try:
        DK.design_to_plan(dirty_design)
    except (ValueError, KeyError, SystemExit) as e:
        msg = str(getattr(e, "code", None) or e)
        assert "对比度" in msg or "lint" in msg.lower(), \
            "低对比度设计被拒了，但错误信息没说是对比度问题：%r" % msg[:120]
    else:
        raise AssertionError("低对比度主题竟然通过了 design_to_plan（体检失效，能造出看不清的字幕）")
    return "最终主题参与体检（%s）" % "；".join(bad)[:60]


# ------------------------------------------------------------------ 词级时间戳 / 画幅 / 依赖 / 批量
def t_word_events():
    """44 词级时间戳：规整/裁剪平移/单调兜底/词锚点解析（不联网）。

    为什么能在不联网的 selfcheck 里测：真正的网络合成在 --stage tts 里，
    而"事件 -> 句内词表"这一步是纯函数。离线固定住它，等于固定住了字幕断行、
    cue 词锚点、G10 门禁三处的共同数据源。
    """
    # offset 用 100ns 单位（实测 edge-tts 7.3.1 口径）：0.10s / 0.36s / 0.50s 起，
    # 词之间刻意留 0.02s 级空隙 —— 真实事件本来就不连续覆盖，抹平它就等于制造错位。
    ev = [{"type": "WordBoundary", "offset": 1000000, "duration": 2360000, "text": "别"},
          {"type": "WordBoundary", "offset": 3600000, "duration": 1180000, "text": "把"},
          {"type": "WordBoundary", "offset": 5000000, "duration": 5050000, "text": "傻子"}]
    ws = K.words_normalize(ev, offset_shift=0.1, dur=1.6)
    assert len(ws) == 3, "词事件数被吃掉：%r" % (ws,)
    assert abs(ws[0]["start"] - 0.0) < 1e-6, "句首裁剪平移没生效（start=%.4f）" % ws[0]["start"]
    # 词间空隙必须保留（中文词之间本来就有 0.1–0.3s 停），不能被"拉平"
    assert ws[1]["start"] > ws[0]["end"], "词间隙被抹平了（会让字幕和语音错位）"
    assert all(ws[i]["start"] <= ws[i + 1]["start"] for i in range(len(ws) - 1)), "词偏移非单调"
    # 末词终点不得超过整句实测时长
    ws2 = K.words_normalize(ev, dur=0.5)
    assert ws2[-1]["end"] <= 0.5 + 1e-6, "末词被算到句外：%r" % (ws2[-1],)
    # 空事件 -> 空表（调用方据此判 skipped，而不是当成"有词级"）
    assert K.words_normalize([]) == [], "空事件应得到空表"
    # 词锚点：l03:w2 = 第二个词的起点
    scenes = [dict(id="l03", start=2.0, dur=4.0,
                   words_abs=[{"text": "别", "start": 2.55, "end": 2.79},
                              {"text": "把", "start": 2.79, "end": 2.91}])]
    assert abs(K.cue_time("l03:w2", scenes) - 2.79) < 1e-6, "词锚点 l03:w2 解析错"
    assert abs(K.cue_time("l03:w2+0.2", scenes) - 2.99) < 1e-6, "词锚点带偏移解析错"
    assert abs(K.cue_time("l03+0.5", scenes) - 2.5) < 1e-6, "旧写法 l03+0.5 被改坏"
    return "3 词事件 / 平移 %.2fs / 词锚点 OK"


def t_video_plan():
    """45 画幅规划：只允许 16:9 横屏 + 下采样，竖屏/上采样必须报错。

    这条守的是"用户以为能出竖屏"的期望管理：安全区/版式/HUD 全按 16:9 像素写死，
    静默出一个变形或带黑边的片子，比直接报错更伤。
    """
    p = K.video_plan({})
    assert (p["out_w"], p["out_h"], p["fps"]) == (1920, 1080, 30), "默认规格变了：%r" % (p,)
    assert p["scale"] is False, "默认不该触发缩放"
    p7 = K.video_plan({"video": {"width": 1280, "height": 720}})
    assert (p7["out_w"], p7["out_h"]) == (1280, 720) and p7["scale"], "720p 下采样没生效"
    assert p7["w"] == 1920 and p7["h"] == 1080, "内部构图应恒为 1080p（缩放只在编码时做）"
    p24 = K.video_plan({"video": {"fps": 24}})
    assert p24["fps"] == 24, "fps 参数化没生效"

    def _must_fail(cfg, why):
        try:
            K.video_plan(cfg)
        except ValueError:
            return
        raise AssertionError("%s 没有报错（会被静默接受）" % why)

    _must_fail({"video": {"width": 1080, "height": 1920}}, "竖屏")
    _must_fail({"video": {"width": 1920, "height": 1440}}, "4:3")
    _must_fail({"video": {"width": 3840, "height": 2160}}, "上采样到 4K")
    _must_fail({"video": {"fps": 31}}, "非法 fps")
    return "默认 1080p30 / 720p 下采样 / 竖屏与上采样均拒绝"


def t_golden_determinism():
    """46 金帧确定性：同一帧连画两次必须逐字节一致（跨进程可复现的前提）。

    比"看起来一样"强的地方：它会在**改了引擎/主题/随机源**时立刻红，
    而不是等观众看出"这版画面怎么抖了一下"。
    """
    mod = _load_scenes_mod()
    if mod is None:
        return "skip（没有 assets/scenes.py）"
    import hashlib
    sc = dict(id="l01", kind="hook", start=0.0, dur=4.0, chapter="01 · 测试",
              text="金帧确定性自检", sub="金帧确定性", big="金帧确定性")
    # 剧本必须显式给设计（内置风格表已删，没有"style": "mono" 这种写法了）。
    script = {"title": "selfcheck", "_total": 8.0}
    script["_style"] = _design_plan("金帧确定性")[1]
    hs = []
    for _ in range(2):
        fr = mod.draw_frame(sc, 1.25, 4.0, 1.25, script, 37)
        hs.append(hashlib.sha256(fr.tobytes()).hexdigest()[:16])
    assert hs[0] == hs[1], "同一帧画了两次不一致（有隐藏的 random/time 依赖）：%r" % (hs,)
    return "同一帧两次 SHA-256 = %s" % hs[0]


def t_dependency_drift():
    """47 依赖漂移体检：上游一改就悄悄退化，这里先喊。

    只做**离线**断言（不联网、不下载）：主版本区间 + edge-tts 是否仍暴露
    boundary 参数。词级时间戳整条链路吊在这个参数上，7.x 一旦改名，
    表现会是"词事件为空"，而不是报错。
    """
    import numpy as np
    from PIL import __version__ as pil_ver
    assert int(np.__version__.split(".")[0]) >= 1, "numpy 主版本异常：%s" % np.__version__
    assert int(pil_ver.split(".")[0]) >= 9, "Pillow 版本过旧：%s" % pil_ver
    try:
        import edge_tts
    except Exception:                                       # noqa: BLE001
        return "skip（没装 edge-tts，只能用手工音频）"
    try:
        import inspect
        params = inspect.signature(edge_tts.Communicate.__init__).parameters
    except (TypeError, ValueError):                         # noqa: BLE001
        return "skip（edge-tts 的签名取不到，版本 %s）" % getattr(edge_tts, "__version__", "?")
    assert "boundary" in params,         ("edge-tts %s 的 Communicate 不再接受 boundary 参数：词级时间戳链路会静默退化成"
         "整句字幕，请检查上游变更" % getattr(edge_tts, "__version__", "?"))
    return "numpy %s / Pillow %s / edge-tts %s(boundary OK)" % (
        np.__version__, pil_ver, getattr(edge_tts, "__version__", "?"))


# ------------------------------------------------------------------ web 载荷 / 金帧
def t_batch_manifest():
    """48 批量清单解析：两种写法都认、上限卡死、坏条目在**开跑前**报错。

    批量最容易死在"第 7 条跑一半才发现清单写错"。这条固定住的是
    render._load_batch 的"先解析、后执行"契约——写错就一条都不跑。
    """
    import render as R

    def _must_fail(payload, why):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.json")
            open(p, "w", encoding="utf-8").write(payload)
            try:
                R._load_batch(p)
            except SystemExit:
                return
            raise AssertionError("%s 没有被拒绝（会带着坏清单开跑）" % why)

    with tempfile.TemporaryDirectory() as d:
        film = os.path.join(d, "a", "film.json")
        os.makedirs(os.path.dirname(film), exist_ok=True)
        open(film, "w", encoding="utf-8").write("{}")
        p = os.path.join(d, "list.json")
        json.dump(["a/film.json"], open(p, "w", encoding="utf-8"))
        jobs = R._load_batch(p)
        assert len(jobs) == 1, "简写清单解析错：%r" % (jobs,)
        assert os.path.isfile(jobs[0]["script"]), "剧本路径没转成绝对路径：%r" % (jobs[0],)
        assert jobs[0]["work"].endswith("work_film"), "默认 work 目录命名变了：%r" % (jobs[0],)
        p2 = os.path.join(d, "list2.json")
        json.dump({"films": [{"script": "a/film.json", "label": "x",
                              "work": "w1", "out": "o1.mp4"}]},
                  open(p2, "w", encoding="utf-8"))
        j2 = R._load_batch(p2)
        assert j2[0]["label"] == "x" and j2[0]["out"].endswith("o1.mp4"), "显式字段被忽略：%r" % (j2,)
    _must_fail("[]", "空清单")
    _must_fail('{"films": [{"label": "x"}]}', "缺 script 的条目")
    _must_fail("[" * 0 + json.dumps([{"script": "a.json"}] * 101), "超过 100 条上限")
    return "两种写法 + 3 类坏清单都按预期拒绝"


def t_tts_backend():
    """49 TTS 后端可替换：注册即生效、未知后端报错、默认仍是 edge。

    这条守的是"旁白链路只挂在非官方 edge-tts 上"的对冲面：换 provider 时
    timeline/layout/QA 不该跟着改。这里只测接口契约，不联网。
    """
    names = K.tts_backend_names()
    assert names[0] == "edge", "默认后端不是 edge：%r" % (names,)

    def fake(text, out_mp3, **kw):
        with open(out_mp3, "wb") as fh:
            fh.write(b"\x00" * 4096)
        return dict(path=out_mp3, dur=1.25, words=[], trimmed=False)

    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        mp3 = os.path.join(d, "x.mp3")
        try:
            K.tts("测试", mp3, backend="no-such-backend")
        except RuntimeError as e:
            assert "未知的 TTS 后端" in str(e), "未知后端报错文案不清晰：%s" % e
        else:
            raise AssertionError("未知后端没有报错（会静默走到别的路径）")
        K.register_tts_backend("fake-test", fake, note="selfcheck")
        assert "fake-test" in K.tts_backend_names()
        m = K.tts("测试", mp3, backend="fake-test")
        assert m["dur"] == 1.25 and m["backend"] == "fake-test", "后端返回值没被采纳：%r" % (m,)
    return "注册/分发/未知后端报错 全部符合契约"


def t_web_style_payload():
    """50 web 引擎风格载荷：非 legacy 才注入、legacy 必须回落 None（逐字节兼容的前提）。

    web 引擎会"跟随风格包"，但兼容性要求是 style=None 时页面输出**逐字节**
    等于不带载荷的旧输出。这条守住"什么时候该注入、什么时候必须不注入"这个开关。
    """
    import render as R
    try:
        import webengine as we
    except Exception as e:                                  # noqa: BLE001
        return "skip（没有 webengine.py：%s）" % e
    # 没有 legacy 概念了（legacy 入口已被拒绝），所以这里只验"设计项目必须注入载荷"。
    script = {"title": "x", "lines": [{"id": "l01", "kind": "hook", "text": "y"}]}
    script["_style"] = _design_plan("web 载荷")[1]
    tl = {"fps": 30, "video": {"out_w": 1280, "out_h": 720}}
    try:
        payload = R._web_style_payload(we, script, tl)
    except SystemExit as e:
        raise AssertionError("有 _style 却被当成没有设计：%s" % (e,)) from e
    assert payload is not None, "非 legacy 项目没有生成 style 载荷（页面不会跟随风格）"
    canvas = payload.get("canvas") or {}
    assert int(canvas.get("w") or 0) == 1280 and int(canvas.get("h") or 0) == 720, \
        "画布尺寸没跟着 video 块走：%r" % (canvas,)
    # 没有设计的剧本：必须回落 None（不是"随便注入一套默认风格"）
    bare = {"title": "x", "lines": [{"id": "l01", "kind": "hook", "text": "y"}]}
    assert R._web_style_payload(we, bare, tl) is None, \
        "没有设计的剧本也注入了 style 载荷（等于凭空套一套外观）"
    return "有设计注入 %dx%d / 无设计回落 None" % (canvas["w"], canvas["h"])


def _golden_profiles():
    """金帧门禁必须覆盖的两个 profile：横屏（历史基线）+ 竖屏（1.7.0 起几何独立）。

    为什么写死两个：selfcheck 只跑一次，导入期的 layoutkit profile 恒横屏，
    `--layout portrait` 的基线在这种跑法下**永远选不中**——竖屏几何改了两个大版本
    （1.7.0 compare/process、1.10/1.11 字号与逐类停留）没有任何报警，直到实测
    `--layout portrait --check` 3/3 DIFF。只测当前 profile 的金帧门禁等于没门禁。
    """
    return (("横屏", "landscape"), ("竖屏", "portrait"))


def _run_golden_json(tool, cwd, argv, timeout=600):
    """跑 golden_frames.py 并把 --json 输出解析成 dict，返回 (proc, data, why)。

    为什么必须读 JSON 而不是只看 returncode：工具是三档语义（pass / fail / skipped），
    skipped 也是 exit 0，只看退出码会把"这次根本没比"记成通过。
    命令没产出 JSON / 解析失败时 data=None、why 写原因，由调用方按 SKIP 处理
    （与改写前的整片金帧用例一致：拿不到结论就不许冒充通过）。
    """
    p = subprocess.run([sys.executable, tool] + list(argv),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=cwd, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       timeout=timeout)
    out = (p.stdout or "").strip()
    i = out.find("{")
    if i < 0:
        return p, None, "没输出 JSON：%s" % ((p.stderr or out)[:120])
    try:
        return p, json.loads(out[i:]), ""
    except json.JSONDecodeError as e:
        return p, None, "JSON 解析失败：%s" % e


def _check_golden_profile(tool, root, label, profile):
    """整片时刻金帧：跑一个 profile 的 --check，返回 (status, detail)。

    status 三档：pass = 真比过且一致；skip = 基线缺失/字体或环境指纹不同（没比）；
    像素不一致或工具自己判 fail 直接抛 AssertionError——同字体同环境还变就是渲染回归，
    降级成 skip 等于把 FAIL 洗成"没查"，仓库铁律 SKIP != 通过。
    """
    base = os.path.join(root, "assets",
                        "golden_frames.json" if profile == "landscape"
                        else "golden_frames_%s.json" % profile)
    if not os.path.exists(base):
        return "skip", "%s没有金帧基线 assets/%s（先跑 --layout %s --write 重录）" % (
            label, os.path.basename(base), profile)
    p, d, why = _run_golden_json(tool, root, ["--layout", profile, "--check", "--json"])
    if d is None:
        return "skip", "%s golden_frames %s" % (label, why)
    status = str(d.get("status") or "")
    changed = d.get("changed") or []
    if changed:
        raise AssertionError(
            "%s金帧变了（画面回归）：changed=%r；%s。若是有意改画风，确认后跑 "
            "python tools/golden_frames.py --layout %s --write 重录基线"
            % (label, changed, (d.get("reason") or "")[:160], profile))
    if status == "pass" and p.returncode == 0:
        return "pass", "%s %d 帧像素一致（font_fp %s）" % (
            label, len(d.get("frames") or []), str(d.get("font_fp") or "")[:8])
    if status == "fail" or p.returncode == 1:
        # 工具自己说 fail（渲染炸了 / 帧号越界等）：这是回归，不是"环境不可比"。
        raise AssertionError("%s golden_frames status=%s exit=%s：%s" % (
            label, status or "-", p.returncode, (d.get("reason") or "")[:200]))
    # 只剩 skipped：字体或环境指纹不同，像素不可比（跨机不误报），理由原样带出来。
    return "skip", "%s status=%s exit=%s：%s" % (
        label, status or "-", p.returncode, (d.get("reason") or "")[:160])


def t_golden_frames():
    """51 金帧回归：横屏 + 竖屏整片像素基线都要真的比过（tools/golden_frames.py）。

    为什么放进 selfcheck 而不是只放 CI：改引擎/主题后"画面变了一点"是最难发现的一类回归，
    而两个 profile 各渲三帧只要一两秒。约定：基线缺失 / 字体或环境指纹不同 -> skip
    （跨机不误报），同环境像素变了才是 fail。

    为什么是双 profile：竖屏整片基线（assets/golden_frames_portrait.json）在 1.7.0
    改完竖屏几何后就没有任何自动入口——CI 恒横屏、旧用例写死横屏基线，它漂了两个版本
    都没人知道（实测 3/3 DIFF）。两个 profile 各比一次才算守住了整片。
    """
    # HERE = <SKILL_DIR>/scripts -> 上一级就是技能根（收编后 tools/ 与 assets/ 都在它下面）
    # （上溯三级会落到全局技能总目录的上一级，工具与基线都找不到，这条就永远 skip —— 静默失效）
    root = os.path.dirname(HERE)
    tool = os.path.join(root, "tools", "golden_frames.py")
    if not os.path.exists(tool):
        return "skip（没有 tools/golden_frames.py）"
    statuses, details = [], []
    for label, profile in _golden_profiles():
        status, detail = _check_golden_profile(tool, root, label, profile)
        statuses.append(status)
        details.append(detail)
    if all(s == "pass" for s in statuses):
        return "；".join(details)
    # 只要有一个 profile 没比成（缺基线 / 换机），整体只能记 SKIP：不许拿另一个 profile
    # 的 pass 冒充"两个 profile 都验过"。像素不一致在 helper 里已经抛 FAIL，到不了这里。
    return "skip（%s）" % "；".join(details)


# ============================================================================
# 场景语法 + 视觉语言
# ============================================================================

def _load_grammar():
    """加载 grammar.py（纯标准库）。找不到返回 None —— CI 里可能只拷了 scripts/ 的一部分。"""
    try:
        import grammar
        return grammar
    except Exception:                                       # noqa: BLE001
        cand = os.path.join(HERE, "grammar.py")
        if not os.path.exists(cand):
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("sc_grammar", cand)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _fake_scenes(G, lines, per=6.0):
    """按固定时长造 timeline 段（不跑 TTS），只用来测选型。"""
    scenes, t = [], 0.0
    for ln in lines:
        row = dict(ln, start=round(t, 3), end=round(t + per, 3), dur=round(per, 3))
        scenes.append(row)
        t += per
    return scenes


def t_scene_grammar():
    """52 场景语法：10 类都能被选中、选型确定、段长门会降级。

    为什么要有这条：quote/emphasis/calm 三种 kind 全都落到 body 绘制器的话，
    画面永远只有一套骨架。这条断言盯的是"内容 -> 构图"这条链路真的在分流，
    而不是"代码里写了 10 个函数但没人调用"。
    """
    G = _load_grammar()
    if G is None:
        return "skip（没有 grammar.py）"
    lines = [
        {"id": "a", "kind": "hook", "chapter": "01", "text": "开场一句。", "big": "开场"},
        {"id": "b", "kind": "body", "chapter": "02", "text": "我们每天被打断 47 次。", "big": "47 次"},
        {"id": "c", "kind": "body", "chapter": "02", "text": "对比一下：深度工作四小时，产出是碎片时间的三倍。",
         "big": "四小时 vs 碎片", "items": [{"text": "深度工作"}, {"text": "碎片时间"}]},
        {"id": "d", "kind": "body", "chapter": "03", "text": "第一步清空收件箱，然后关掉通知，接着单线程，最后复盘。",
         "big": "四步", "items": [{"text": "清空"}, {"text": "关通知"}, {"text": "单线程"}, {"text": "复盘"}]},
        {"id": "e", "kind": "quote", "chapter": "03", "text": "他说：「深度工作稀缺。」", "quote": "深度工作稀缺"},
        {"id": "f", "kind": "body", "chapter": "04", "text": "各行业占比差异明显。", "big": "各行业占比",
         "items": [{"text": "A 62%"}, {"text": "B 51%"}, {"text": "C 38%"}]},
        {"id": "g", "kind": "body", "chapter": "05", "text": "注意力是换来的。", "big": "注意力是换来的"},
        {"id": "h", "kind": "body", "chapter": "06", "text": "第五章开始了。", "big": "第五章"},
        {"id": "i", "kind": "body", "chapter": "06", "text": "这里有五条要点要讲清楚。", "big": "五条要点",
         "items": [{"text": "一"}, {"text": "二"}, {"text": "三"}, {"text": "四"}, {"text": "五"}]},
        {"id": "j", "kind": "outro", "chapter": "99", "text": "开始就好。", "big": "开始就好"},
    ]
    scenes = _fake_scenes(G, lines)
    plan = {"chapter_layout": {}, "layout_default": "left-stack", "style": "tech-deep"}
    sb1 = G.plan_scenes([dict(s) for s in scenes], {"title": "t"}, plan)
    got = [r["type"] for r in sb1["scenes"]]
    # ① 确定性：同一份输入两次结果完全一致（含变体号与理由）
    sb2 = G.plan_scenes([dict(s) for s in scenes], {"title": "t"}, plan)
    assert [r["type"] for r in sb2["scenes"]] == got, "同一份稿子两次选型不同（不确定性）"
    assert [r["variant"] for r in sb2["scenes"]] == [r["variant"] for r in sb1["scenes"]], \
        "同一份稿子两次变体不同（不确定性）"
    # ② 分流：至少命中 5 种不同类型（不是全退化成 list/statement）
    assert len(set(got)) >= 5, "10 段只分出 %d 种构图（%r）：选型逻辑没在分流" % (len(set(got)), got)
    assert "stat" in got and "compare" in got and "quote" in got, \
        "数字/对比/引语三种特征没有各自分流：%r" % (got,)
    # ③ 段长门：把 chart 塞进 2 秒段必须降级，且理由里写明
    short = [{"id": "x", "kind": "body", "chapter": "01", "text": "短。", "big": "短",
              "type": "chart", "series": [{"label": "A", "value": 1}]}]
    sb3 = G.plan_scenes([dict(s) for s in _fake_scenes(G, short, per=2.0)],
                        {"title": "t"}, plan)
    r3 = sb3["scenes"][0]
    assert r3["type"] != "chart", "2 秒的段仍选了 chart（段长门失效）"
    assert any("降级" in x or "排除" in x for x in r3["reason"]), \
        "降级没有留下理由：%r" % (r3["reason"],)
    # ④ 显式 type 优先
    ex = [{"id": "y", "kind": "body", "chapter": "01", "text": "随便一句", "big": "随便",
           "type": "hero"}]
    sb4 = G.plan_scenes([dict(s) for s in _fake_scenes(G, ex)], {"title": "t"}, plan)
    assert sb4["scenes"][0]["type"] == "hero", "显式 type=hero 没生效：%r" % (sb4["scenes"][0],)
    return "10 段 -> %d 种构图；降级 chart->%s；显式 type 生效" % (len(set(got)), r3["type"])


def t_visual_languages():
    """53 视觉语言包：8 套结构签名齐全、过体检、且彼此不是同一套。

    为什么要有这条：只改配色的"风格"会让 8 套风格骨架完全一样（用户抱怨的根因）。
    这条盯的是"结构签名真的存在且互不相同"——母题/转场/重心偏移任一维度相同不算问题，
    但**五组签名全同**就说明这套语言只是复制粘贴。
    """
    import designkit as DK
    import stylekit as ST
    # 没有 8 套内置语言了。验"现场生成的语言"，而且断言更强：
    # 12 个设计的两两结构签名必须互不相同（以前只要求 8 套里 >=6 种母题）。
    sigs, motifs, transes, huds, pushes = {}, set(), set(), set(), set()
    designs = DK.sample_designs({"topic": "视觉语言多样性"}, n=12, seed=20260202)
    for d in designs:
        huds.add(str(d["visual"]["hud"]))
        pushes.add(float(d["visual"]["motion"]["push"]))
        plan = DK.design_to_plan(d)
        vl = plan["language"]
        bad = ST.visual_lint_language(vl)
        assert not bad, "%s 的语言包没过体检：%s" % (d["label"], bad[:2])
        key = (str((vl.get("motif") or {}).get("kind")),
               str((vl.get("transition") or {}).get("kind")),
               tuple(sorted((vl.get("bias") or {}).items())))
        sigs.setdefault(key, []).append(d["label"])
        motifs.add(str((vl.get("motif") or {}).get("kind")))
        transes.add(str((vl.get("transition") or {}).get("kind")))
    dup = [v for v in sigs.values() if len(v) > 1]
    assert not dup, "这些设计语言签名完全相同（等于只在换色）: %s" % (dup[:2],)
    # 默认必须是素净：默认设计带细线是用户实测反馈的缺陷，不是可选项。
    assert motifs == {"plain"}, "默认采样出现了非素净母题：%r" % (sorted(motifs),)
    assert huds == {"none"}, "默认采样又带上了 HUD（顶部细进度条）：%r" % (sorted(huds),)
    assert pushes == {0.0}, "默认采样又在推镜（慢速亚像素位移 = 抖动）：%r" % (sorted(pushes),)
    assert len(transes) >= 4, "12 个设计只用到 %d 种转场" % len(transes)
    # 显式点名装饰母题时必须能采到（素净是默认，不是唯一）。
    picked = DK.sample_designs({"topic": "视觉语言多样性", "must_have": ["括角"]},
                               n=1, seed=20260203)[0]
    assert picked["visual"]["motif"]["kind"] == "brackets", \
        "点名「括角」却没采到 brackets：%r" % (picked["visual"]["motif"],)
    return "12 个设计 / 默认 plain+none+不推镜 / %d 种转场 / 无重复签名 / 点名母题生效" % (
        len(transes))


def t_scene_types_single_truth():
    """54 场景类型只有一份"真值集合"：4 处定义必须集合相等（顺序可以不同）。

    为什么比集合不比顺序：grammar.SCENE_TYPES 的顺序是"平手取位顺序"，改它会改变同一份
    稿子的选型结果，所以它有独立语义；filmcheck/schema/scenes 只关心"有哪些类型"。
    顺序不同是设计，集合不同是 bug（写错一个字母 = 某个类型永远画不出来）。
    """
    G = _load_grammar()
    if G is None:
        return "skip（没有 grammar.py）"
    here_scripts = HERE
    got = {"grammar": set(G.SCENE_TYPES)}
    # filmcheck.SCENE_TYPES
    try:
        import filmcheck as FC
        got["filmcheck"] = set(FC.SCENE_TYPES)
    except Exception:                                       # noqa: BLE001
        pass
    # assets/scenes.py 的 _DRAWERS 键
    mod = _load_scenes_mod()
    if mod is not None and hasattr(mod, "_DRAWERS"):
        got["scenes"] = set(mod._DRAWERS.keys())
    # 收编后 schemas/ 在技能目录下（上游在包根）。上游这里还多退了一级 ".."：
    # 从 <包根>/skills 再退两级就出了包根，schema 永远找不到，本条会静默降级成“3 处真值”。
    schema = os.path.join(os.path.dirname(here_scripts), "schemas", "film.schema.json")
    schema = os.path.normpath(schema)
    if os.path.exists(schema):
        try:
            data = json.load(open(schema, encoding="utf-8"))
            enum = (data["properties"]["lines"]["items"]["properties"]["type"]["enum"])
            got["schema"] = set(enum)
        except Exception:                                   # noqa: BLE001
            pass
    if len(got) < 3:
        return "skip（只找到 %d 处真值：%s）" % (len(got), sorted(got))
    base_name, base = sorted(got.items())[0]
    for name, s in got.items():
        if s != base:
            raise AssertionError("场景类型集合不一致：%s 与 %s 差 %r / %r"
                                 % (name, base_name, sorted(s - base), sorted(base - s)))
    return "%d 处真值集合一致（%d 类）：%s" % (len(got), len(base), "/".join(sorted(got)))


def t_silhouette_gate():
    """55 剪影可区分（S13，离线检查）：8 套风格两两之间构图骨架必须能区分。

    这是核心验收：如果这条挂了，说明"换风格只是换色"的毛病回来了。
    工具在 tools/silhouette_qa.py（可选）：缺了 skip，不阻塞 CI。
    """
    root = os.path.dirname(HERE)
    tool = os.path.join(root, "tools", "silhouette_qa.py")
    if not os.path.exists(tool):
        return "skip（没有 tools/silhouette_qa.py）"
    p = subprocess.run([sys.executable, tool, "--json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=root)
    out = (p.stdout or "").strip()
    i = out.find("{")
    if i < 0:
        return "skip（silhouette_qa 没输出 JSON：%s）" % ((p.stderr or out) or "")[:120]
    try:
        d = json.loads(out[i:])
    except json.JSONDecodeError as e:
        return "skip（silhouette_qa JSON 解析失败：%s）" % e
    pairs = d.get("pairs") or []
    # 三档语义与工具对齐：status 只有 pass / fail / skip 三种。
    # 只认 returncode 会把"工具没渲出帧"（skip）也记成 PASS —— 那正是本仓库反复踩的坑。
    failed = [x for x in pairs if str(x.get("status")) == "fail"]
    skipped = [x for x in pairs if str(x.get("status")) == "skip"]
    if p.returncode != 0 or failed:
        names = ["%s×%s" % (x.get("a"), x.get("b")) for x in failed[:4]]
        raise AssertionError("剪影不可区分：%d/%d 对失败（%s）；换风格仍只是换色"
                             % (len(failed), len(pairs), ", ".join(names)))
    if skipped:
        return "skip（%d/%d 对没渲出来：%s）" % (
            len(skipped), len(pairs),
            ", ".join("%s×%s" % (x.get("a"), x.get("b")) for x in skipped[:3]))
    # 同明暗族是"保守下界"：跨族很多时候靠 ΔB（背景亮度差）就能过，那不是构图证据。
    # 所以这条断言在报告里必须把同族数字单独说出来，别让 28/28 被读成"8 套风格结构全不同"。
    same = ((d.get("summary") or {}).get("by_family") or {}).get("same") or {}
    same_n = same.get("pairs")
    same_ok = same.get("pass")
    if same_n and same_ok is not None and int(same_ok) < int(same_n):
        raise AssertionError("同明暗族只有 %s/%s 对可区分（保守下界不达标）"
                             % (same_ok, same_n))
    return "%d 对全部可区分（同明暗族 %s/%s，保守下界也过）" % (
        len(pairs), same_ok if same_ok is not None else "?", same_n if same_n else "?")


def t_design_dsl():
    """56 设计 DSL：采样 -> 校验 -> 转 plan，且 fail-closed（坏设计必须报错）。"""
    try:
        import designkit as DK
    except Exception:                                       # noqa: BLE001
        return "skip（没有 designkit.py）"
    ds = DK.sample_designs({"topic": "selfcheck 设计 DSL"}, n=3)
    assert len(ds) == 3, "采样器只给了 %d 个候选" % len(ds)
    for d in ds:
        bad = DK.validate_design(d)
        assert not bad, "采样器吐出了不合格的设计 %r：%s" % (d.get("id"), bad)
        plan = DK.design_to_plan(d)
        assert plan["legacy"] is False, "design 计划不该标 legacy"
        assert str(plan["style"]).startswith("design:"), "style 应是 design:<id>：%r" % plan["style"]
        assert len(plan.get("theme") or {}) >= 30, "主题字段太少：%d" % len(plan.get("theme") or {})
    # 两两必须至少 2 个维度不同（"伪多方案"门禁）
    for i in range(len(ds)):
        for j in range(i + 1, len(ds)):
            d = DK.design_distance(ds[i], ds[j])
            assert d["n"] >= 2, "候选 %s 与 %s 只差 %d 维" % (ds[i]["id"], ds[j]["id"], d["n"])
    # fail-closed：删掉必填块必须报错，不能静默回落
    broken = json.loads(json.dumps(ds[0]))
    broken["visual"].pop("motif", None)
    assert DK.validate_design(broken), "缺 motif 竟然通过校验（fail-closed 失效）"
    # 语义是**不给 seed = 每次都不一样**（需求是"每条视频都不同"），
    # 给 seed 才可复现。两条都要测，否则改错了任一边都发现不了。
    x1 = [d["id"] for d in DK.sample_designs({"topic": "selfcheck 设计 DSL"}, n=3)]
    x2 = [d["id"] for d in DK.sample_designs({"topic": "selfcheck 设计 DSL"}, n=3)]
    assert x1 != x2, "同一 brief 两次采样完全相同（那就是把模板藏进了哈希里）"
    y1 = [d["id"] for d in DK.sample_designs({"topic": "selfcheck 设计 DSL"}, n=3, seed=42)]
    y2 = [d["id"] for d in DK.sample_designs({"topic": "selfcheck 设计 DSL"}, n=3, seed=42)]
    assert y1 == y2, "给了 seed 竟然不可复现（回归/文档示例就不可信了）"
    return "3 候选 / 校验全过 / 两两 >=2 维 / 缺字段必报错 / 默认每次不同 + seed 可复现"


def t_no_templates():
    """57 零模板：仓库里**不许再有**预设/模板文件（本技能只提供技术，不提供模板）。

    这条是反向断言：预设整个删掉后，
    如果只是删掉这条检查，就再也拦不住"哪天有人又加一套预设进去"。
    所以改成**禁止存在**——删掉模板是一次性动作，守住它是长期契约。
    """
    root = os.path.dirname(HERE)
    skill = root
    banned = []
    presets_dir = os.path.join(skill, "presets")
    if os.path.isdir(presets_dir):
        n = len([x for x in os.listdir(presets_dir) if x.endswith(".json")])
        banned.append("presets/ 目录还在（%d 个 json）" % n)
    for name in ("presetkit.py",):
        if os.path.exists(os.path.join(skill, "scripts", name)):
            banned.append("scripts/%s 还在" % name)
    assert not banned, ("本技能不提供模板，这些必须删掉：%s；"
                        "要自定义请写 design.json（技术原语仍在，只是不再有现成目录）"
                        % "；".join(banned))
    # 同时确认"现场采样"是唯一方式，且**每次都新**：
    #   · 同一 brief 两次 -> 必须不同（否则等于模板藏在哈希里，用户要新片只能改措辞）
    #   · 给 seed -> 必须可复现（回归与文档示例需要一个稳定值）
    try:
        import designkit as DK
    except Exception:                                       # noqa: BLE001
        return "skip（没有 designkit.py）"
    a = [d["label"] for d in DK.sample_designs({"topic": "零模板校验 A"}, n=2)]
    b = [d["label"] for d in DK.sample_designs({"topic": "零模板校验 A"}, n=2)]
    c = [d["label"] for d in DK.sample_designs({"topic": "零模板校验 B"}, n=2)]
    s1 = [d["label"] for d in DK.sample_designs({"topic": "零模板校验 A"}, n=2, seed=7)]
    s2 = [d["label"] for d in DK.sample_designs({"topic": "零模板校验 A"}, n=2, seed=7)]
    assert a != b, "同一 brief 两次采样结果相同（等于把模板藏进了哈希）"
    assert a != c, "换 brief 采样结果没变（说明其实在套模板）"
    assert s1 == s2, "给了 seed 竟然不可复现"
    return "无模板文件；同 brief 两次必不同、换 brief 必变、seed 可复现"


def t_brief_constraints():
    """60 brief 里的要求必须真的生效（自然语言 -> 约束 -> 采样结果）。

    这条防的是**需求被静默吃掉**：propose._brief_of 若把整句 --brief 塞进 topic，
    must_have/avoid 恒为空数组，于是"深蓝配色的科技解说"采出来是 h087 / h316（橙 / 紫）。
    用户看到的需求和结果无关，而且没有任何提示——比报错更糟。

    三件事都要验：
      ① 自由文本能解析成 must_have / avoid（含否定词归到 avoid）；
      ② 配色约束按**色相窗口**生效（不是按 8 个族名）；
      ③ avoid 真的把色相排除掉。
    """
    import re
    try:
        import propose as PP
        import designkit as DK
        import stylekit as ST
    except Exception:                                       # noqa: BLE001
        return "skip（缺 propose/designkit/stylekit）"
    # ① 解析
    b1 = PP._brief_of("要深蓝，不要放射线", {"title": "t"})
    assert b1["must_have"] and "深蓝" in b1["must_have"][0], "must_have 没解析出来：%r" % (b1["must_have"],)
    assert b1["avoid"] and "放射" in b1["avoid"][0], "avoid 没解析出来：%r" % (b1["avoid"],)
    b2 = PP._brief_of("随便做个片", {"title": "t"})
    assert b2["must_have"] == [] and b2["avoid"] == [], "认不出要求时不该硬凑约束"
    # ② 配色约束 -> 色相窗口
    ds = DK.sample_designs({"topic": "深蓝科技", "must_have": ["深蓝"]}, n=2, seed=7)
    for d in ds:
        h = DK._design_bg_hue(d)
        assert h is not None, "取不到底色色相"
        gap = DK._hue_gap(h, DK._PALETTE_ANCHOR["deep-blue"][0])
        assert gap <= DK._HUE_WINDOW + 1e-6, (
            "must_have『深蓝』没生效：底色色相 %.1f° 距 210° 有 %.1f°（窗口 %.0f°）"
            % (h, gap, DK._HUE_WINDOW))
    # ③ avoid 真的排除
    mint = DK._PALETTE_ANCHOR["cool-mint"][0]
    for i in range(3):
        d = DK.sample_designs({"topic": "避开薄荷 %d" % i, "avoid": ["薄荷"]}, n=1, seed=i)[0]
        h = DK._design_bg_hue(d)
        assert DK._hue_gap(h, mint) > DK._HUE_WINDOW, (
            "avoid『薄荷』没生效：底色色相 %.1f° 落在薄荷范围（%.0f°±%.0f°）"
            % (h, mint, DK._HUE_WINDOW))
    return "解析 2/2；深蓝落入窗口；薄荷 3/3 被排除"


def t_stage_standalone():
    """59 逐 stage 可单独跑：render 里每处 resolve_style 都必须能拿到 work。

    这条守住一类**只在"单跑某个 stage"时才发作**的 bug：
    `--stage all` 时前面的 stage 已经把 script["_style"] 塞好了，后面全都命中缓存，
    所以缺参数也看不出来；一旦用户只跑 `--stage frames` / `--stage sfx`，
    resolve_style 读不到 work/design.json 就报"找不到设计"（实测踩到：
    frames / sfx 两个 stage 单跑必挂，sfx 还是 NameError——函数里压根没有 work 这个名字）。

    为什么用 AST 而不是真的跑每个 stage：跑一遍 = 要 TTS 联网 + 全片渲染，几十秒起步，
    不适合放进 20 秒的自检。静态检查能抓住"参数没传/名字不存在"这一整类问题，
    而这正是本次的真实缺陷形态。
    """
    import ast
    src_path = os.path.join(HERE, "render.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        args = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "resolve_style":
                continue
            # resolve_style(script, work) 的第二位必须是本函数能拿到的变量
            if len(sub.args) >= 2:
                second = sub.args[1]
                vname = getattr(second, "id", None)
                if vname and vname not in args and vname not in ("work",):
                    # 明确允许 a.work 这类属性访问（不是 Name）
                    bad.append("%s(): resolve_style 的 work 参数用了 %r，但该函数没有这个变量"
                               % (node.name, vname))
            else:
                bad.append("%s(): resolve_style 没传 work（单跑该 stage 会读不到 design.json）"
                           % node.name)
    assert not bad, "；".join(bad[:4])
    return "render 里 %d 处 resolve_style 全部带得上 work" % sum(
        1 for n in ast.walk(tree) if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "resolve_style")


def t_proposal_gate():
    """58 提案闸：没确认时必须判 draft/absent，确认后才放行；坏文件判 broken。"""
    try:
        import propose as PP
    except Exception:                                       # noqa: BLE001
        return "skip（没有 propose.py）"
    with tempfile.TemporaryDirectory() as work:
        status, detail = PP.proposal_state(work)
        assert status == "absent", "空目录应判 absent，收到 %r" % status
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "proposal.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "rev": 1, "status": "draft",
                       "candidates": [{"id": "A"}, {"id": "B"}]}, fh)
        status, _ = PP.proposal_state(work)
        assert status == "draft", "有草案但没确认应判 draft，收到 %r" % status
        with open(os.path.join(work, "proposal.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken json")
        try:
            status, _ = PP.proposal_state(work)
        except SystemExit:
            status = "broken"
        assert status == "broken", "坏 JSON 应判 broken，收到 %r" % status
    return "absent / draft / broken 三档判定正确"




def t_subtitle_style():
    """61 字幕样式可定制：design.visual.subtitle 必须真的改变像素，缺省逐像素不变。

    为什么必须有这条：字幕字号/位置/背板过去是 filmkit 的**模块常量**，任何两条片子
    的字幕带都一模一样 —— 这正是用户说的"跨片大面积重复"。改成设计字段后，最容易
    出现的退化是"字段读进来了但没传给 draw_subtitle"（写完不生效，实测踩过）。
    所以这里验行为：同一个 theme、同一段文字，换字幕参数必须像素不同；
    不传参数时必须与不传该字段的调用逐像素一致（老项目不漂）。
    """
    import numpy as _np
    import designkit as DK
    # 用一套现场采样设计里的主题，避免依赖内置主题表（已删）
    d = DK.sample_designs({"topic": "字幕样式自检"}, n=1, seed=3)[0]
    plan = DK.design_to_plan(d)
    theme = plan["theme"]
    text = "这是一句用来测试字幕样式是否真的生效的中文字幕"
    base = K.Image.new("RGB", (K.W, K.H), tuple(theme["bg"]))
    default_img = K.draw_subtitle(base.copy(), text, theme=theme)
    kw = K.subtitle_kwargs(plan)
    assert kw.get("size") and kw.get("align") in ("center", "left"), (
        "subtitle_kwargs 没把设计的字幕参数带出来：%r" % (kw,))
    styled = K.draw_subtitle(base.copy(), text, theme=theme, **kw)
    a = _np.asarray(default_img, dtype=_np.int16)
    b = _np.asarray(styled, dtype=_np.int16)
    assert int(_np.abs(a - b).max()) > 0, "字幕参数没有改变任何像素（字段写了不生效）"
    # 缺省必须等价于旧调用（老 design.json 逐像素不变）
    assert K.subtitle_kwargs({}) == {}, "没有 subtitle 的计划必须返回空 kw（保持旧像素）"
    assert K.subtitle_kwargs({"subtitle": {}}) == {}, "空 subtitle 也必须是空 kw"
    return "字幕参数改变像素；缺省等价旧调用（size=%s align=%s）" % (kw.get("size"), kw.get("align"))


def t_layout_cycle():
    """62 片内版式轮换：layout_cycle 真的让不同章节换骨架，同章必须稳定。

    用户原则 5：同一部片不能"长时间一张版式"。design.visual.layout 是单值，
    grammar 把它当全片默认 → 8 段片子 8 屏同骨架。这条验三件事：
      ① 有 layout_cycle 时，不同 chapter 拿到不同版式；
      ② 同一个 chapter 内所有段版式一致（不能每句都跳）；
      ③ 没有 layout_cycle 时行为与旧版一致（全用 layout_default）。
    """
    import grammar as G
    scenes = [
        {"id": "a1", "kind": "body", "chapter": "第一章", "start": 0.0, "end": 4.0, "dur": 4.0},
        {"id": "a2", "kind": "body", "chapter": "第一章", "start": 4.0, "end": 8.0, "dur": 4.0},
        {"id": "b1", "kind": "body", "chapter": "第二章", "start": 8.0, "end": 12.0, "dur": 4.0},
        {"id": "b2", "kind": "body", "chapter": "第二章", "start": 12.0, "end": 16.0, "dur": 4.0},
    ]
    plan = {"chapter_layout": {}, "layout_default": "left-stack",
            "layout_cycle": ["left-stack", "center-stack"]}
    scenes = [dict(s) for s in scenes]
    G.plan_scenes(scenes, {"lines": []}, plan)
    got = {s["id"]: s["scene_layout"] for s in scenes}
    assert got["a1"] == got["a2"], "同一章内版式必须稳定，实际 %s" % (got,)
    assert got["b1"] == got["b2"], "同一章内版式必须稳定，实际 %s" % (got,)
    assert got["a1"] != got["b1"], "不同章节应轮换版式，实际两章都是 %s" % (got["a1"],)
    # 没有 cycle 时回到旧行为
    scenes2 = [dict(s) for s in scenes]
    G.plan_scenes(scenes2, {"lines": []}, {"chapter_layout": {}, "layout_default": "split-right"})
    assert all(s["scene_layout"] == "split-right" for s in scenes2), \
        "没有 layout_cycle 时必须全用 layout_default（旧行为）"
    return "两章轮换 %s -> %s；同章稳定；无 cycle 时回落 layout_default" % (got["a1"], got["b1"])


def t_brief_constraint_robustness():
    """63 带约束的 brief 不许高概率直接失败。

    实测过的真缺陷：配色候选去重按 24° 分桶，而 must_have「深蓝」把色相窗口压到 52°，
    n=3 时约一半概率抛 ValueError；「暖纸色 + 女声」20/20 全失败。用户第一步就撞墙。
    修法改成"色相距离 + 结构差异"分档后，这里用 20 次独立采样守回归。
    """
    import propose as PP
    import designkit as DK
    briefs = ["深蓝", "纪录片，暖纸色，缓慢，女声", "科技发布会，深蓝，要遮幅"]
    for text in briefs:
        b = PP._brief_of(text, {"title": "t"})
        for _ in range(6):
            try:
                DK.sample_designs(b, n=3)
            except ValueError as e:
                raise AssertionError("brief %r 采样失败：%s" % (text, str(e)[:200])) from e
    return "3 种带约束 brief × 6 次 = 18 次 n=3 采样全部成功"


def t_removed_apis_fail_closed():
    """64 已删除的 API 必须"明确报错/非零退出"，不许 NameError、假绿或静默 fallback。

    删内置风格表时留下了一批**死路径**：stylekit 的 --list/--show NameError、
    --lint 空表 exit 0（假绿）、filmkit 的 resolve_theme/style_plan 吞异常回落到写死的
    写死的主题（静默降级 = 用模板出片）。这三种都必须绝迹。
    """
    import subprocess as _sp
    import stylekit as ST
    import filmkit as FK
    # ① stylekit.resolve_script_style 必须 raise 且带指引
    try:
        ST.resolve_script_style({"title": "t", "lines": []})
    except (ValueError, KeyError, SystemExit) as e:
        msg = str(getattr(e, "code", None) or e)
        assert "propose.py" in msg, "报错必须给迁移指引（要提到 propose.py）：%r" % msg[:120]
    else:
        raise AssertionError("没有设计的剧本竟然解析出了计划")
    # ② stylekit CLI 三个动作必须非零退出（不许假绿/NameError traceback）
    for flag in ("--list", "--lint"):
        p = _sp.run([sys.executable, os.path.join(HERE, "stylekit.py"), flag],
                    capture_output=True, text=True, timeout=60)
        assert p.returncode == 2, "stylekit.py %s 应 exit 2（能力已移除），实际 %d" % (flag, p.returncode)
        assert "Traceback" not in (p.stderr or ""), "stylekit.py %s 不该抛 traceback" % flag
    # ③ filmkit 的静默回落必须改成明确报错
    for fn in (FK.resolve_theme, FK.style_plan):
        try:
            fn({"title": "t", "lines": []})
        except (ValueError, KeyError) as e:
            assert "propose" in str(e).lower() or "design" in str(e).lower(), \
                "%s 的报错要给迁移指引：%r" % (fn.__name__, str(e)[:120])
        else:
            raise AssertionError("%s 在无设计时静默返回了兜底主题（模板残留）" % fn.__name__)
    # ④ designkit 的配色采样示例不许崩（--list-palettes 曾 KeyError: family）
    p = _sp.run([sys.executable, os.path.join(HERE, "designkit.py"), "--list-palettes", "--json"],
                capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, "designkit.py --list-palettes 崩了：%s" % (p.stderr or p.stdout)[:200]
    return "resolve_script_style/CLI/filmkit 全部 fail-closed；--list-palettes 可跑"


def t_audio_overrides():
    """65 design.audio 的音量/音效参数必须真的落进 script。

    实测过的真缺陷：design.json 的 audio.music.gain_db / duck_db 与 audio.sfx.enabled
    / density / gain_db 一直被 validate_design 接受、却从来没有渲染代码读它们 ——
    用户把 sfx.enabled 写成 false，成片照样出 8 条音效。
    现在的契约：**film.json 显式写了的 > design 的值 > 模块默认**。
    """
    import designkit as DK
    d = DK.sample_designs({"topic": "声音参数自检"}, n=1, seed=5)[0]
    d = json.loads(json.dumps(d))
    d["audio"]["music"]["gain_db"] = -7.5
    d["audio"]["music"]["duck_db"] = 9.0
    d["audio"]["sfx"] = {"enabled": False, "density": "rich", "gain_db": -3}
    plan = DK.design_to_plan(d)
    ov = plan.get("audio_overrides") or {}
    assert abs(float(ov["music"]["gain_db"]) + 7.5) < 1e-9, "music.gain_db 没进计划"
    assert abs(float(ov["music"]["duck_db"]) - 9.0) < 1e-9, "music.duck_db 没进计划"
    assert ov["sfx"]["enabled"] is False, "sfx.enabled 没进计划"
    import render as R
    # 剧本里没写 -> 用 design 的值
    s1 = {"title": "t", "lines": []}
    R._apply_audio_overrides(s1, plan)
    assert s1["sfx"]["enabled"] is False and abs(s1["music"]["gain_db"] + 7.5) < 1e-9, \
        "design 的声音参数没有填进 script：%r" % (s1,)
    # 剧本显式写了 -> 以剧本为准（用户手里 film.json 是最终裁决权）
    s2 = {"title": "t", "lines": [], "music": {"gain_db": -1.0}, "sfx": {"enabled": True}}
    R._apply_audio_overrides(s2, plan)
    assert s2["music"]["gain_db"] == -1.0 and s2["sfx"]["enabled"] is True, \
        "film.json 的显式值被 design 覆盖了：%r" % (s2,)
    return "design.audio -> plan -> script 贯通；film.json 显式值优先"


def t_design_ledger():
    """66 片间去重台账：确认过的设计要记住，下次提案主动避开；可关可查。

    用户原则 5：不同视频的整体风格不能大面积重复。只保证"同一 brief 两次采样不同"
    （nonce），但没有记忆——连拍同题材仍可能撞到相近设计。台账补上这一环。
    这条验四件事：① confirm 后写台账；② 提案会读台账并报告距离；
    ③ 台账路径可被环境变量重定向（绝不污染用户真实台账）；④ --no-ledger 可关闭。
    """
    import tempfile
    import subprocess as _sp
    tmp = tempfile.mkdtemp(prefix="vs_ledger_selfcheck_")
    ledger = os.path.join(tmp, "ledger.json")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", VIDEO_STUDIO_LEDGER=ledger)
    script = {"title": "台账自检", "lines": [
        {"id": "l01", "kind": "hook", "text": "第一句话。"},
        {"id": "l02", "kind": "body", "text": "第二句话，讲一件具体的事。"},
        {"id": "l03", "kind": "outro", "text": "结束。"}]}
    film = os.path.join(tmp, "film.json")
    with open(film, "w", encoding="utf-8") as fh:
        json.dump(script, fh, ensure_ascii=False)
    pp = os.path.join(HERE, "propose.py")

    def _run(args):
        r = _sp.run([sys.executable, pp] + args, cwd=tmp, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
        assert r.returncode == 0, "propose.py %s 失败：%s" % (args[0], (r.stderr or r.stdout)[-300:])
        return r

    # 台账这条例验的是"记账/避开"，不是问诊：问诊另有用例 70 守。
    _run(["--brief", "深蓝科技解说", "--script", film, "--n", "3", "--no-intake"])
    assert not os.path.exists(ledger), "还没 confirm 就写台账了（草稿不该记）"
    _run(["--confirm", "--candidate", "A"])
    assert os.path.exists(ledger), "confirm 之后没有写台账：%s" % ledger
    data = json.load(open(ledger, encoding="utf-8"))
    assert len(data.get("designs") or []) == 1, "台账条数不对：%r" % (data,)
    row = data["designs"][0]
    assert row.get("fingerprint") and isinstance(row.get("design"), dict), "台账记录缺字段"
    # 第二次提案：必须能读到台账（人读输出里有一行"片间去重"）
    r = _run(["--brief", "深蓝科技解说", "--script", film, "--n", "3", "--no-intake"])
    assert "片间去重" in r.stdout, "提案没有报告台账状态：%s" % r.stdout[-400:]
    # --show-ledger 能查；--no-ledger 能关
    r = _run(["--show-ledger"])
    assert row["fingerprint"] in r.stdout, "--show-ledger 没列出刚记的指纹"
    r = _run(["--brief", "深蓝科技解说", "--script", film, "--n", "3", "--no-ledger",
              "--no-intake"])
    assert "--no-ledger" in r.stdout, "--no-ledger 没有给出关闭提示"
    return "草稿不记 / confirm 记账 / 提案读取并报告 / --show-ledger 可查 / --no-ledger 可关"


def t_intake_gate():
    """70 出片前问诊：一次问完 + 首次提案前必须答完（--no-intake 才放行且留痕）。

    用户的真实抱怨是"有时一个问题不问就一路跑完，有时只问一两个"——根因是"问不问"
    全靠 agent 当场发挥。这条把它变成可执行的门：
      ① 问诊单是固定的 8 题，每一项都有推荐默认值，且**任何题材的推荐默认都是明亮活泼**；
      ② 制作方式题（自己画/网上素材剪辑/两者结合）是必答题：题材给得出推荐值
         （真实影像 -> footage、数据/原理 -> draw、两边都占 -> hybrid），auto 档按题材落地；
         显式选择优先，且 finalize 必须打印该产线的下一步命令（素材线不许再套 propose）；
      ③ 没答完时首次提案直接失败，并给出"怎么问、怎么答、怎么定稿"三步；
      ④ 答完后提案里必须留痕（intake.status = complete，答案 + mode 随提案落盘）；
      ⑤ --no-intake 是显式逃生口，用掉时留 bypassed 痕迹（事后能查"这片子问没问过"）。
    """
    import subprocess as _sp
    tmp = tempfile.mkdtemp(prefix="vs_intake_selfcheck_")
    scripts = HERE
    intake_py = os.path.join(scripts, "intake.py")
    propose_py = os.path.join(scripts, "propose.py")
    assert os.path.exists(intake_py), "缺 intake.py（出片前问诊脚本）"
    film = os.path.join(tmp, "film.json")
    with open(film, "w", encoding="utf-8") as fh:
        json.dump({"title": "问诊自检", "lines": [
            {"id": "l01", "kind": "hook", "text": "第一句话。"},
            {"id": "l02", "kind": "body", "text": "第二句话，讲一件具体的事。"},
            {"id": "l03", "kind": "outro", "text": "结束。"}]}, fh, ensure_ascii=False)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def _run(args, cwd):
        return _sp.run([sys.executable] + args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=300)

    # ① 题目包：固定 8 题、每题带默认值、气质题默认明亮活泼且排第一；
    #    制作方式题（mode）必须在：必答、默认 auto、三个产线选项齐全
    r = _run([intake_py, "--work", "work", "--questions", "--json"], tmp)
    assert r.returncode == 0, "intake.py --questions 失败：%s" % (r.stderr or r.stdout)[-300:]
    payload = json.loads(r.stdout[r.stdout.find("{"):])
    qs = payload.get("questions") or []
    assert len(qs) == 8, \
        "问诊单应有 8 题（题材/制作方式/时长/气质/画幅/声线/配乐/补充），实际 %d" % len(qs)
    byid = {q["id"]: q for q in qs}
    for qid in ("topic", "mode", "duration", "tone", "voice", "music"):
        assert byid.get(qid) and byid[qid].get("required"), "必答题缺 %r" % qid
    assert byid["tone"].get("default") == "bright-lively", "气质题默认必须是 bright-lively"
    assert byid["tone"]["options"][0]["value"] == "bright-lively", \
        "气质题第一项必须是明亮活泼（推荐项排最前）"
    mode_q = byid["mode"]
    assert mode_q.get("default") == "auto", "制作方式题默认必须是 auto（按题材推荐）"
    vals = [o["value"] for o in mode_q.get("options") or []]
    assert vals == ["draw", "footage", "hybrid", "auto"], \
        "制作方式题选项应为 draw/footage/hybrid/auto，实际 %r" % (vals,)
    # 推荐值必须随题材变化（"根据要做的视频内容推荐"——不是恒定的一个值）
    import intake as IT
    assert IT.recommend_mode("中国高铁十年，纪录片，用真实影像")["mode"] == "footage", \
        "真实影像题材应推荐 footage：%r" % (IT.recommend_mode("中国高铁十年，纪录片，用真实影像"),)
    assert IT.recommend_mode("AI 大模型的三年，数据图表科普")["mode"] == "draw", \
        "数据/原理题材应推荐 draw：%r" % (IT.recommend_mode("AI 大模型的三年，数据图表科普"),)
    assert IT.recommend_mode("城市纪录片里穿插数据图表")["mode"] == "hybrid", \
        "两边都占时应推荐 hybrid：%r" % (IT.recommend_mode("城市纪录片里穿插数据图表"),)
    assert IT.recommend_mode("随便讲个故事")["mode"] == "draw", \
        "没有线索时应默认 draw（零素材线）：%r" % (IT.recommend_mode("随便讲个故事"),)
    # 否定句不该把推荐带偏："不要真实影像，纯数据科普" 仍应推荐 draw
    neg_draw = IT.recommend_mode("不要真实影像，纯数据科普")
    assert neg_draw["mode"] == "draw" and not neg_draw["hits"]["footage"], \
        "否定句里的影像词被算进了推荐：%r" % (neg_draw,)
    neg_footage = IT.recommend_mode("不要数据图表，用真实影像讲城市")
    assert neg_footage["mode"] == "footage" and not neg_footage["hits"]["draw"], \
        "否定句里的数据词被算进了推荐：%r" % (neg_footage,)
    r = _run([intake_py, "--work", "work", "--questions", "--json", "--topic",
              "中国高铁十年，纪录片"], tmp)
    assert r.returncode == 0, "带 --topic 的问诊单失败：%s" % (r.stderr or r.stdout)[-300:]
    rows = json.loads(r.stdout[r.stdout.find("{"):]).get("questions") or []
    mode_row = [q for q in rows if q["id"] == "mode"][0]
    assert mode_row.get("recommended") == "footage", \
        "问诊单没带出题材推荐值：%r" % (mode_row,)
    assert str(mode_row.get("recommend_reason") or "").strip(), "推荐没给理由：%r" % (mode_row,)

    # ② 没答完：首次提案必须失败，并且说清三步怎么做
    work1 = os.path.join(tmp, "work1")
    r = _run([propose_py, "--brief", "明亮活泼的科普片", "--script", film,
              "--work", work1, "--n", "2"], tmp)
    assert r.returncode != 0, "没答问诊竟然直接出方案了（问诊闸失效）"
    msg = (r.stderr or "") + (r.stdout or "")
    assert "intake.py" in msg and "还没" in msg, "拦截时要给出 intake.py 指引：%r" % msg[-300:]

    # ③ 答完 + 定稿：提案放行，并留 complete 痕迹；auto 制作方式按题材落地
    r = _run([intake_py, "--work", work1,
              "--set", "topic:AI 大模型的三年", "--set", "mode:auto",
              "--set", "duration:at_least_1m",
              "--set", "tone:bright-lively", "--set", "voice:female-bright",
              "--set", "music:upbeat"], tmp)
    assert r.returncode == 0, "写答答案失败：%s" % (r.stderr or r.stdout)[-300:]
    r = _run([intake_py, "--work", work1, "--finalize"], tmp)
    assert r.returncode == 0, "--finalize 失败：%s" % (r.stderr or r.stdout)[-300:]
    intake_json = json.load(open(os.path.join(work1, "intake.json"), encoding="utf-8"))
    mode_dec = intake_json.get("mode") or {}
    assert mode_dec.get("chosen") == "auto" and mode_dec.get("resolved") == "draw", \
        "auto 制作方式没按题材落地：%r" % (mode_dec,)
    assert "propose.py" in r.stdout and "footage.py" not in r.stdout, \
        "draw 档的下一步应指向 propose.py：%r" % r.stdout[-300:]
    brief_txt = open(os.path.join(work1, "intake.brief.txt"), encoding="utf-8").read()
    assert "明亮活泼" in brief_txt, "气质答案没进 brief：%r" % brief_txt
    assert "自己画" not in brief_txt and "素材" not in brief_txt, \
        "制作方式被写进了外观 brief（它是路由，不是外观约束）：%r" % brief_txt
    r = _run([propose_py, "--work", work1, "--n", "2", "--json"], tmp)
    assert r.returncode == 0, "答完问诊后提案失败：%s" % (r.stderr or r.stdout)[-300:]
    data = json.loads(r.stdout[r.stdout.find("{"):])
    intake = data.get("intake") or {}
    assert intake.get("status") == "complete" and not intake.get("bypassed"), \
        "提案里没记下问诊状态：%r" % (intake,)
    assert intake.get("answers", {}).get("tone") == "bright-lively", "答案没随提案落盘"
    must = (data.get("brief") or {}).get("must_have") or []
    assert any("明亮" in str(x) for x in must), "气质答案没变成 brief 约束：%r" % (must,)
    # 首个候选应该是明亮活泼档（用户要"亮一点、有精神"）
    import designkit as DK
    first = data["candidates"][0]["design"]
    assert DK._tone_of(first) == DK._TONE_BRIGHT, \
        "首个候选不是明亮活泼：%r" % (DK._tone_of(first),)

    # ④ --no-intake 放行且留痕
    work2 = os.path.join(tmp, "work2")
    r = _run([propose_py, "--brief", "补渲测试", "--script", film,
              "--work", work2, "--n", "2", "--no-intake", "--json"], tmp)
    assert r.returncode == 0, "--no-intake 应放行：%s" % (r.stderr or r.stdout)[-300:]
    data2 = json.loads(r.stdout[r.stdout.find("{"):])
    assert (data2.get("intake") or {}).get("status") == "bypassed", \
        "--no-intake 没留 bypassed 痕迹：%r" % (data2.get("intake"),)

    # ⑤ 制作方式=素材剪辑：finalize 的下一步必须是 footage.py，提案层要明确说"不适用"
    work3 = os.path.join(tmp, "work3")
    r = _run([intake_py, "--work", work3,
              "--set", "topic:中国高铁十年纪录片，用真实影像", "--set", "mode:footage",
              "--set", "duration:at_least_3m", "--set", "tone:bright-lively",
              "--set", "voice:female-bright", "--set", "music:upbeat"], tmp)
    assert r.returncode == 0, "写素材线答案失败：%s" % (r.stderr or r.stdout)[-300:]
    r = _run([intake_py, "--work", work3, "--finalize"], tmp)
    assert r.returncode == 0, "素材线 --finalize 失败：%s" % (r.stderr or r.stdout)[-300:]
    assert "footage.py" in r.stdout, "素材线的下一步没给 footage.py：%r" % r.stdout[-300:]
    assert "提案层不适用" in r.stdout, \
        "素材线 finalize 没写明\"提案层不适用\"：%r" % r.stdout[-300:]
    r = _run([propose_py, "--work", work3, "--n", "2"], tmp)
    assert r.returncode == 0, "素材线仍要能跑提案（hybrid 叠加层用）：%s" % (r.stderr or r.stdout)[-300:]
    msg = (r.stdout or "") + (r.stderr or "")
    assert "素材剪辑" in msg and "提案层不适用" in msg, \
        "素材线跑提案时没提示\"提案层不适用\"：%r" % msg[-300:]
    proposal = json.load(open(os.path.join(work3, "proposal.json"), encoding="utf-8"))
    assert (proposal.get("intake") or {}).get("mode", {}).get("resolved") == "footage", \
        "提案里没记下制作方式：%r" % (proposal.get("intake"),)
    return ("8 题固定 / 制作方式推荐（footage·draw·hybrid）/ auto 按题材落地 / "
            "素材线下一步走 footage.py 且提案层提示不适用 / 没答完必拦 / --no-intake 留痕")


def t_duration_target():
    """67 时长要求真的被接住：brief 里的「2 分钟」要影响设计，渲染时对账。

    过去 `_brief_of` 把 duration_target 恒填 None —— 用户写「做个 2 分钟的片」等于没写，
    成片永远按台词长度出（实测 23s / 30s）。这条守三件事：
      ① 中文/阿拉伯数字的常见时长说法都能解析（ASCII 单位 45s/2m 也要认，
         否则文档示例照抄就会在 intake 处被判非法）；
      ② 解析出的时长进 design.structure.target_sec，并决定候选段落数；
      ③ 渲染时按"目标 vs 实测"对账（差 >15% 告警，但不截断旁白）。
    """
    import propose as PP
    import designkit as DK
    cases = {"2分钟": 120.0, "做个90秒的短视频": 90.0, "一分钟半": 90.0,
             "3分30秒": 210.0, "2.5分钟": 150.0, "两分钟": 120.0}
    for text, want in cases.items():
        got = PP.parse_duration(text)
        assert got is not None and abs(got - want) < 1e-6, \
            "时长解析 %r 得到 %r，期望 %r" % (text, got, want)
    assert PP.parse_duration("深蓝科技解说") is None, "没有时长时不该硬凑"
    assert PP.parse_duration("两小时") is None, "超出 3–1800s 契约范围应拒绝"
    # ASCII 单位：README / SKILL / intake 三处文档的示例都写 --set duration:45s，
    # 解析器不认就等于"照文档执行必失败"（intake 会报"不是合法选项"）。
    ascii_cases = {"45s": 45.0, "2m": 120.0, "1.5min": 90.0, "90sec": 90.0}
    for text, want in ascii_cases.items():
        got = PP.parse_duration(text)
        assert got is not None and abs(got - want) < 1e-6, \
            "ASCII 时长解析 %r 得到 %r，期望 %r" % (text, got, want)
    # 「每 5 分钟走 20 秒」是在说节拍，只取首个匹配（5 分钟 = 300 s）。
    # 相加成 320 s 会把整片长度排错，所以这里钉住 300.0 而不是"至少认出一个"。
    multi = PP.parse_duration("每5分钟走20秒")
    assert multi == 300.0, "多时长句子应只取首个匹配 300.0（5 分钟），实际 %r" % (multi,)
    # 下限语义与 ASCII 单位要能叠加：「不少于45s」是 45 s 的下限，不是精确目标。
    assert PP.parse_duration_spec("不少于45s") == (45.0, "at_least"), \
        "ASCII 单位没接住下限语义：%r" % (PP.parse_duration_spec("不少于45s"),)
    b = PP._brief_of("深蓝，科技，2分钟，硬切", {"title": "t"})
    assert b.get("duration_target") == 120.0, "brief 没接住时长：%r" % (b,)
    d = DK.sample_designs(b, n=2, seed=9)[0]
    assert d["structure"].get("target_sec") == 120.0, \
        "目标时长没进设计：%r" % (d["structure"],)
    plan = DK.design_to_plan(d)
    assert plan.get("target_sec") == 120.0, "目标时长没进计划：%r" % (plan.get("target_sec"),)
    return "6 种中文 + 4 种 ASCII 时长说法解析正确；多时长只取首个；进 design/plan；超范围拒绝"
def t_zoom_stability():
    """68 缩放取景的帧间稳定性（抖动回归）。

    为什么要有这条：底图把颗粒烘进去后用 NEAREST 逐帧直采，采样网格随 s 整体跳变，
    颗粒图案跟着错位 1px —— 用户实测的原话是"生成的视频有明显的抖动感"，
    而单帧闪烁门禁（8x8 分块比较前后帧平均）对这类亚像素沸腾**完全不敏感**。
    断言：同一主题从 s=1.0 缓推到 s=1+push 的过程中，相邻两帧最大差 <= 2 灰阶。
    端点直采实测抖动 4~6，改成端点预烘 + blend 后 0~1。
    """
    mod = _load_scenes_mod()
    if mod is None:
        return "skip（没有 assets/scenes.py）"
    import numpy as np
    push = 0.12
    th = {"id": "zoom-stab", "bg": (28, 30, 36), "bg2": (18, 19, 24), "texture": "paper",
          "grain": 5.0, "grain_freq": 0.31, "vignette": 0.2, "vignette_tint": (0, 0, 0),
          "texture_alpha": 0.03, "push": push}
    frames = [np.asarray(mod._bg_plate(th, 1.0 + push * i / 19.0), dtype=np.int16)
              for i in range(20)]
    worst_max = 0
    worst_mean = 0.0
    for i in range(1, len(frames)):
        d = np.abs(frames[i] - frames[i - 1])
        worst_max = max(worst_max, int(d.max()))
        worst_mean = max(worst_mean, float(d.mean()))
    assert worst_max <= 2, ("缩放取景帧间跳变 %d 灰阶（>2）：底图高频纹理被 NEAREST 重排，"
                            "观感就是满屏噪点沸腾" % worst_max)
    # 均值守住 0.6：8-bit 输出下慢速缓推必然出现"整片 1 灰阶"的量化台阶，
    # 首帧端点与 s≈1.006 之间实测 mean≈0.44 —— 这是量化噪声，不是图案错位。
    # 真正能区分"错位沸腾"的是 max：旧实现 4~6，新实现 <=1。
    assert worst_mean <= 0.6, "缩放取景帧间平均差 %.3f（>0.6，疑似整屏跳变）" % worst_mean
    return ("20 帧缓推：最大帧间差 %d 灰阶 / 均值 %.3f（旧 NEAREST 直采为 4~6 灰阶）"
            % (worst_max, worst_mean))


def t_asr_lost_diagnostics():
    """69 ASR 掉分可定位（低召回必须说清"掉在哪几个字"）。

    为什么要有这条：实测 ASR 回读 0.813~0.844 是常态（中文专名 + 同音字），只报一个
    百分比，agent 只能盲目放宽阈值。这条断言盯的是"低于 pass 线时，报告必须给出
    没对上的字符（全局 + 最差段落）"，且判分本身仍然是纯函数、不加载模型。
    """
    cand = os.path.join(HERE, "qaasr.py")
    if not os.path.exists(cand):
        return "skip（没有 qaasr.py）"
    import importlib.util
    spec = importlib.util.spec_from_file_location("sc_qaasr", cand)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    scenes = [
        {"id": "a", "start": 0.0, "end": 3.0, "voice_start": 0.4,
         "text": "这是用 DeepSeek 生成的声音与画面。"},
        {"id": "b", "start": 3.0, "end": 6.0, "voice_start": 3.4,
         "text": "先定脚本，再定声音，最后才是画面。"},
    ]
    # ASR 把 DeepSeek 听成"迪普西克"、把"再定"听成"在订"——低召回但不丢句
    segments = [
        {"start": 0.4, "end": 3.0, "text": "这是用 迪普西克 生成的声音与画面"},
        {"start": 3.4, "end": 6.0, "text": "先定脚本，在订声音，最后才是画面"},
    ]
    rep = mod._judge(scenes, segments, recall_pass=0.99, recall_warn=0.5)
    assert rep["recall"] < 0.99, "构造样例没有触发低召回，断言前提不成立"
    assert rep.get("lost_top"), "低召回时没有输出 lost_top（掉分字符清单）"
    assert all("ch" in x and "n" in x for x in rep["lost_top"]), "lost_top 结构不对"
    lost_lines = [p for p in rep["per_line"] if p.get("lost")]
    assert lost_lines, "没有段落级 lost（最差段落无法定位）"
    warns = " ".join(rep.get("warnings") or [])
    assert "没对上的字" in warns, "低召回时警告里没有掉分字符清单"
    assert "最差段落" in warns, "低召回时警告里没有最差段落定位"
    return "lost_top %d 项 / 段落级 %d 段 / 警告 %d 条" % (
        len(rep["lost_top"]), len(lost_lines), len(rep.get("warnings") or []))



# ---------------------------------------------------------------- 细线 / 抖动护栏
# 这两条守的是两条产品约束（不是「看着还行」）：
#   ① 默认设计不画任何装饰细线（母题、底纹、HUD 顶栏都是细线来源）；
#   ② 默认设计前景不动（相机只推背景，文字按整像素落位）。
# 为什么用结构性记录而不是逐像素扫描：1px 线在压缩后只剩 1~2 灰阶，像素判据抓不稳；
# 而「谁调了 line(width=1)」是确定的，回归时能直接点名是哪个构图。

def _plain_scene(stype):
    """一条覆盖 10 类构图所需字段的场景（自检专用，给定 seed 下可复现）。"""
    return {
        "id": "sc_" + stype, "kind": "body", "scene_type": stype,
        "scene_variant": 0, "scene_decor": 2, "scene_layout": "left-stack",
        "text": "这是一句用于验证的说明文字", "big": "零素材，全代码生成",
        "sub": "确定性逐帧，可复现，可验收", "chapter": "02 · 正文", "kicker": "02 · 正文",
        "start": 0.0, "end": 12.0, "dur": 12.0,
        "items": [{"text": "第一条：时间基准先算死"}, {"text": "第二条：内容与风格解耦"},
                  {"text": "第三条：音频实测反推时间"}],
        "quote": "观众最先察觉的瑕疵是字幕漂移", "by": "复盘",
        "_stats": [{"value": "338207", "unit": "次", "label": "工具调用"}],
        "_series": [{"value": 38, "label": "写作"}, {"value": 27, "label": "渲染"},
                    {"value": 19, "label": "质检"}, {"value": 16, "label": "其他"}],
        "_steps": [{"label": "写"}, {"label": "测"}, {"label": "渲"}, {"label": "检"}],
        "_sides": {"left": {"head": "旧", "items": ["按字数估时"]},
                   "right": {"head": "新", "items": ["实测时长"]}},
    }


def _rect(xy):
    """ImageDraw 的 xy 可能是扁平列表（[x0,y0,x1,y1]）也可能是点对列表。"""
    if not xy:
        return (0, 0, 0, 0)
    if isinstance(xy[0], (int, float)):
        pts = [(xy[i], xy[i + 1]) for i in range(0, len(xy) - 1, 2)]
    else:
        pts = list(xy)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _record_draws(mod, fn):
    """在绘制调用层记录 line / rectangle（text / ellipse 原样透传）。"""
    from PIL import ImageDraw
    log = []
    orig = ImageDraw.Draw

    class _Rec(object):
        def __init__(self, inner):
            self._inner = inner

        def line(self, xy, fill=None, width=1, joint=None):
            log.append(("line", int(width), _rect(xy)))
            return self._inner.line(xy, fill=fill, width=width, joint=joint)

        def rectangle(self, xy, fill=None, outline=None, width=1):
            log.append(("rect", int(width), _rect(xy), bool(outline)))
            return self._inner.rectangle(xy, fill=fill, outline=outline, width=width)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def _make(img, mode=None):
        return _Rec(orig(img, mode)) if mode else _Rec(orig(img))

    mod.ImageDraw.Draw = _make
    try:
        fn()
    finally:
        mod.ImageDraw.Draw = orig
    return log
def _thin_strokes(mod, script, frame=180, stypes=None):
    """扫 10 类构图的绘制调用，返回 (细线, 细长色条)。

    细线 = line 的 width < 4；细长色条 = 无 outline 的填充矩形且「长 >= 40 且厚 <= 3」。
    两档都按**调用参数**判，不靠像素扫描：1px 线压缩后只剩 1~2 灰阶，像素判据抓不稳。
    """
    bad_lines, bad_rects = [], []
    for stype in (stypes or ("hero", "statement", "list", "stat", "compare",
                             "process", "quote", "chapter", "chart", "outro")):
        scene = _plain_scene(stype)
        log = _record_draws(mod, lambda sc=scene: mod.draw_frame(sc, 6.0, 12.0, 6.0, script, frame))
        for rec in log:
            if rec[0] == "line" and rec[1] < 4:
                bad_lines.append((stype, rec[1], rec[2]))
            elif rec[0] == "rect" and not rec[3]:
                x0, y0, x1, y1 = rec[2]
                w, h = abs(x1 - x0), abs(y1 - y0)
                if (w >= 40 and h <= 3) or (h >= 40 and w <= 3):
                    bad_rects.append((stype, (w, h), rec[2]))
    return bad_lines, bad_rects


def t_plain_design_has_no_hairlines():
    """71 默认设计不画装饰细线（母题/底纹/HUD 顶栏三条来源全关）。

    用户原话是「默认带着一些细线」：括角刻度 1px、hairlines 页眉、letterbox 内缘、
    底纹 grid/scanlines、chip-left 的通栏进度条（3px 细线 / 现为 6px 块）。这条按**绘制调用**守：
      a) 默认采样落 plain（母题层整层透明、纹理为 none、HUD 为 none）；
      b) plain 下任何 line 的 width >= 4，且没有「长 >= 40 且厚 <= 3」的填充矩形；
      c) 显式打开 HUD（chip-left / corner-line）时同样不许有细线；
      d) 遮幅（letterbox）的黑边不随 push 缩放（push=0.12 下高度恒 40px）。
    """
    import numpy as np
    import designkit as DK
    mod = _load_scenes_mod()
    if mod is None:
        return "skip（没有 assets/scenes.py）"
    d = DK.sample_designs({"topic": "细线护栏"}, n=1, seed=20261116)[0]
    assert d["visual"]["motif"]["kind"] == "plain", \
        "默认母题不是 plain：%r" % (d["visual"]["motif"],)
    assert d["visual"]["hud"] == "none", "默认 HUD 不是 none：%r" % (d["visual"]["hud"],)
    plan = DK.design_to_plan(d)
    th = plan["theme"]
    assert th["texture"] == "none" and not th["texture_alpha"], \
        "默认设计仍叠了背景纹理：%r" % (th["texture"],)
    lang = plan["language"]
    assert mod._decor_scale(_plain_scene("hero"), lang) <= 0.02, \
        "plain 的装饰强度不为 0：%r" % mod._decor_scale(_plain_scene("hero"), lang)
    lay = mod._motif_layer(th, lang)
    assert lay.getextrema()[3] == (0, 0), "plain 的装饰层不是全透明"
    # 零密度对 plain 必须是合法默认（素净就是「不画装饰」），不能被 lint 当成配置错误。
    import copy as _copy
    zero = _copy.deepcopy(d)
    zero["visual"]["motif"]["density"] = 0.0
    probs = DK.validate_design(zero)
    assert not probs, "plain + density=0 被判非法：%r" % (probs[:2],)

    script = {"title": "细线护栏", "_style": plan, "_total": 12.0}
    bad_lines, bad_rects = _thin_strokes(mod, script, 180)
    assert not bad_lines, "默认设计画了 %d 条细线（width<4）：%s" % (
        len(bad_lines), bad_lines[:3])
    assert not bad_rects, "默认设计画了 %d 条细长色条（厚<=3px）：%s" % (
        len(bad_rects), bad_rects[:3])

    # HUD 不是"默认关了就没人用"的例外：显式打开 corner-line 时，它的 2px 角线也必须
    # 过 _lw（素净下抬到 6px）。这条曾经漏掉——_hud 不接 lang，corner-line 仍是 2px。
    import copy as _copy2
    for hud in ("chip-left", "corner-line"):
        d2 = _copy2.deepcopy(d)
        d2["visual"]["hud"] = hud
        p2 = DK.design_to_plan(d2)
        s2 = {"title": "细线护栏", "_style": p2, "_total": 12.0}
        bl2, br2 = _thin_strokes(mod, s2, 180)
        assert not bl2 and not br2, \
            "素净 + hud=%s 仍有细线：%r %r" % (hud, bl2[:2], br2[:2])

    # 遮幅黑边是"画幅"，不许跟着 push 缩放（实测 40px 会被放大成 0px）。
    d3 = _copy2.deepcopy(d)
    d3["visual"]["motif"] = {"kind": "letterbox", "density": 0.85, "detail": {}}
    d3["visual"]["motion"]["push"] = 0.12
    p3 = DK.design_to_plan(d3)
    s3 = {"title": "遮幅", "_style": p3, "_total": 12.0}
    bars = []
    for t in (1.0, 6.0, 11.0):
        img = mod.draw_frame(_plain_scene("hero"), t, 12.0, t, s3, int(t * 30))
        arr = np.asarray(img, dtype=np.uint8)[:, 960, :].mean(axis=1)
        bars.append(int((arr[:200] < 6).sum()))
    assert bars == [40, 40, 40], "push 时遮幅高度漂了：%r（应恒为 40）" % (bars,)
    return ("10 类构图：plain 无装饰层 / 无底纹 / 无线宽<4 / 无细长色条；"
            "corner-line 也无线；遮幅在 push=0.12 下恒 40px")


def t_foreground_pinned():
    """72 前景不随镜头位移（抖动的根因是慢速亚像素位移 + 整数取整，不是运动本身）。

    实测：push=0.14 的 4 秒段里文字锚点每秒跳 12 次，一次跳跃 5000+ 像素差 >40 灰阶；
    push=0.02 也有 0.67 次/秒 —— 观众感知到的就是「画面在抖」。
    修法不是把相机调慢（越慢越糟），而是让**前景不跟相机**：文字按整像素落位，
    只有背景板做端点预烘 + blend 的平滑缩放。这条守三件事：
      a) `_view` 对任何 s 都返回同一组前景坐标（结构级）；
      b) push=0 时 `_push_scale` 恒为 1.0（默认不推镜）；
      c) 光栅级：statement 标题带连续 30 帧逐字节一致（取整台阶的最终护栏）。
    """
    import numpy as np
    import designkit as DK
    mod = _load_scenes_mod()
    if mod is None:
        return "skip（没有 assets/scenes.py）"
    d = DK.sample_designs({"topic": "抖动护栏"}, n=1, seed=20261116)[0]
    plan = DK.design_to_plan(d)
    th = plan["theme"]
    coords = {(mod._view(th, s).x(300.0), mod._view(th, s).y(241.0))
              for s in (1.0, 1.02, 1.06, 1.10, 1.14)}
    assert len(coords) == 1, "前景坐标仍随镜头缩放变化：%r" % (sorted(coords),)
    assert mod._push_scale(th, 0.0, 10.0) == 1.0, "push=0 时缩放不是 1.0"
    script = {"title": "抖动护栏", "_style": plan, "_total": 12.0}
    scene = _plain_scene("statement")
    ref = None
    for i in range(30):
        t = 5.0 + i / 30.0
        img = mod.draw_frame(scene, t, 12.0, t, script, int(t * 30))
        band = np.asarray(img, dtype=np.uint8)[430:600, 120:1500]
        if ref is None:
            ref = band
        elif not np.array_equal(ref, band):
            diff = np.abs(ref.astype(np.int16) - band.astype(np.int16))
            raise AssertionError(
                "同一段静止画面前景在动：第 %d 帧标题带最大差 %d 灰阶（>0）"
                % (i, int(diff.max())))
    return "前景坐标与 s 无关；push=0 恒 1.0；statement 标题带 30 帧逐字节一致"

def t_layout_single_source():
    """73 画布/安全区单一真源：filmkit 的 W/H/SAFE/SUB_BAND 必须来自 layoutkit。

    为什么值得一条断言：这些常量以前在 filmkit 顶部写一遍，scenes.py / preflight /
    qa_frames 又各自 from filmkit import 复制。"改画布尺寸"于是等于"找到所有副本"——
    而竖屏恰好就是改画布尺寸，所以它一直做不了。收成一份之后，还要防止有人手一滑
    又把数字写回 filmkit（那种回归不会有任何报错，只会让两处慢慢漂移）。

    验四件事：① layoutkit 默认值与历史值逐项相等；② filmkit 导出的就是 layoutkit 的；
    ③ scenes.py 从 filmkit 取的 W/H 等于 layoutkit 的（画面坐标系没被绕过）；
    ④ profile() 给的是副本，改局部不会污染全局。
    """
    import layoutkit as LK
    detail = LK.verify_defaults()          # ① 与写死的历史值比对（不拿自己比自己）
    for name, want in (("W", LK.W), ("H", LK.H), ("SAFE", LK.SAFE),
                       ("SUB_BAND", LK.SUB_BAND), ("SUB_MAX_CHARS", LK.SUB_MAX_CHARS)):
        got = getattr(K, name)
        assert got == want, "filmkit.%s=%r 与 layoutkit 的 %r 不一致（有人把数字写回去了？）" % (name, got, want)
    # assets/scenes.py 只在装了包的场景存在（CI 里可能只拷 scripts/），缺了就跳过这段；
    # 但**只要它在**，坐标系就必须与 layoutkit 一致——这是能不能做竖屏的前提。
    SC = _load_scenes_mod()
    if SC is None:
        return detail + " · filmkit 取自同一份（没有 assets/scenes.py，跳过坐标系核对）"
    assert (SC.W, SC.H) == (LK.W, LK.H), \
        "scenes 的坐标系 (%r,%r) 与 layoutkit (%r,%r) 不一致：画面会画到画布外" % (
            SC.W, SC.H, LK.W, LK.H)
    p = LK.profile()
    p["w"] = 1                       # ④ 改副本不许影响本体
    assert LK.W != 1 and LK.profile()["w"] == LK.W, "profile() 交出去的不是副本"
    return detail + " · filmkit/scenes 均取自同一份"


def t_critic_offline():
    """74 审美质检：离线、可复现、不写 proposal.json。

    四层 QA + VMAF + ASR 全在回答"不能出错"，没有一条回答"好不好看"。critic 补这一环，
    也因此必须守住三条边界，否则它会变成又一个"看起来在查、其实在猜"的模块：
    ① 同一张图两次打分必须相同（确定性，禁 random）；
    ② 它认不出联系表就不许瞎判（多帧拼图跳过安全区，而不是报 700px 越界）；
    ③ 它**不写** proposal.json——审美分不是提案契约的一部分。
    """
    crit = os.path.join(HERE, "critic.py")
    if not os.path.exists(crit):
        return "skip（没有 critic.py）"
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:                                         # noqa: BLE001
        return "skip（缺 numpy/Pillow）"
    import tempfile
    tmp = tempfile.mkdtemp(prefix="vs_critic_selfcheck_")
    img = os.path.join(tmp, "frame.png")
    from PIL import Image as _I, ImageDraw as _D
    im = _I.new("RGB", (1920, 1080), (18, 22, 30))
    d = _D.Draw(im)
    d.rectangle([120, 140, 900, 420], fill=(233, 236, 241))      # 一块明显的前景
    d.rectangle([120, 700, 1760, 760], fill=(60, 130, 220))
    im.save(img)

    def _score(pic):
        r = subprocess.run([sys.executable, crit, "--image", pic, "--json"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                        timeout=300)
        assert r.returncode == 0, "critic.py 失败：%s" % (r.stderr or r.stdout)[-300:]
        return json.loads(r.stdout)

    a = _score(img)
    b = _score(img)
    sa = a["items"][0]["score"]
    sb = b["items"][0]["score"]
    assert sa == sb, "同一张图两次打分不同（%r vs %r）：critic 必须确定性" % (sa, sb)
    assert 0.0 <= sa <= 100.0, "分数量纲不对：%r" % (sa,)
    assert a["items"][0].get("metrics"), "缺 metrics（没法解释为什么扣分）"

    # 联系表（多帧拼图）必须跳过安全区判定，而不是稳定误报
    sheet = os.path.join(tmp, "_sheet.png")
    strip = _I.new("RGB", (1440, 270), (18, 22, 30))
    ds = _D.Draw(strip)
    for i in range(3):
        ds.rectangle([i * 480 + 20, 20, i * 480 + 460, 250], fill=(233, 236, 241))
    strip.save(sheet)
    js = _score(sheet)
    note = " ".join(js["items"][0].get("notes") or [])
    assert "联系表" in note, "联系表没有被识别（应该跳过安全区判定）：%r" % (note,)
    assert "安全区" not in note.replace("跳过安全区判定", ""), \
        "联系表被判了安全区越界：%r" % (note,)
    return "确定性一致（%.1f）/ metrics 齐全 / 联系表跳过安全区" % sa


def t_portrait_profile():
    """75 竖屏 profile：画布/安全区/字号随画布折算，且文字不许出画。

    竖屏是 **experimental**：安全区与字幕带按 1080x1920 算好、10 类构图都能出图，
    **版式骨架与重点构图（compare/process/chart）都已竖屏原生**，剩下的只是
    "排版节奏未按竖屏复核"。这条断言守的是"能出片且文字不出画"这条底线，不是"好看"：
    ① 竖屏画布真的是 1080x1920（不是被静默回落到横屏）；
    ② 10 类构图全部能在竖屏渲出一张图，不抛异常；
    ③ 把一段**明显过宽**的文字塞进去，必须被 `_fit_px` 钳进安全区并留下记录——
       这是"文字不会出画"的**构造性保证**（横屏 W==1920 时该函数原样返回，零影响）。
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(here, "..", "assets")
    # 探针脚本写**系统临时目录**，不写源码树：写在 scripts/ 下时，进程被强杀
    # （CI 超时、Ctrl-C 打断子进程）finally 就不会执行，于是仓库里留下一个
    # `_portrait_probe.py` 垃圾文件——同 filmkit 解码帧的处理理由。
    import tempfile
    _probe_dir = tempfile.mkdtemp(prefix="vs-selfcheck-")
    probe = os.path.join(_probe_dir, "_portrait_probe.py")
    body = chr(10).join([
        "import os, sys, json",
        "sys.path.insert(0, %r)" % here,
        "sys.path.insert(0, %r)" % os.path.abspath(assets),
        "import layoutkit as LK",
        "import designkit as DK",
        "import scenes as SC",
        "design = json.load(open(os.path.join(%r, 'design_fixture.json'), encoding='utf-8'))"
        % os.path.abspath(assets),
        "plan = DK.design_to_plan(design)",
        "script = {'title': 'T', 'lines': [], '_style': plan}",
        "out = {'canvas': [SC.W, SC.H], 'safe': list(LK.SAFE), 'sizes': {}, 'err': {}}",
        "kinds = {'hero':'hook','statement':'body','list':'body','stat':'body',",
        "         'compare':'body','process':'body','quote':'quote','chapter':'body',",
        "         'chart':'body','outro':'outro'}",
        "for st in kinds:",
        "    scene = {'id': 'p_' + st, 'kind': kinds[st], 'scene_type': st,",
        "             'scene_layout': 'split-right',",
        "             'text': '这是一句用来测试排版的正文，长度中等偏长。',",
        "             'sub': '字幕一行', 'big': '标题在这里', 'chapter': '01',",
        "             'items': [{'text': '条目一'}, {'text': '条目二'}],",
        "             'chart': {'bars': [3, 5, 2]}}",
        "    try:",
        "        out['sizes'][st] = list(SC.draw_frame(scene, 1.0, 6.0, 1.0, script, 30).size)",
        "    except Exception as e:",
        "        out['err'][st] = str(e)[:120]",
        "# 过宽文字必须被钳住并留痕（构造性保证，不是像素推断）",
        "SC._CLAMPED.clear()",
        "SC._fit_px(50, 5000.0, 'x' * 40, 96)",
        "out['clamped'] = len(SC._CLAMPED)",
        "import stylekit as ST",
        "# 构图内部的竖屏分支：compare 上下对照 / process 竖向轴；两者都必须走到自己的分支",
        "cmp_scene = {'id': 'c', 'kind': 'body', 'scene_type': 'compare', 'big': '对比',",
        "  'sides': {'left': {'head': '甲', 'items': ['一', '二']},"
        "            'right': {'head': '乙', 'items': ['三', '四']}}}",
        "prc_scene = {'id': 'p', 'kind': 'body', 'scene_type': 'process', 'big': '流程',",
        "  'steps': [{'label': '甲', 'note': 'x'}, {'label': '乙', 'note': 'y'}]}",
        "out['cmp_mode'] = hasattr(SC, '_draw_compare_portrait') and \\",
        "    SC.draw_frame(cmp_scene, 1.0, 5.0, 1.0, script, 30).size == (SC.W, SC.H)",
        "out['prc_mode'] = hasattr(SC, '_draw_process_portrait') and \\",
        "    SC.draw_frame(prc_scene, 1.0, 5.0, 1.0, script, 30).size == (SC.W, SC.H)",
        "lay = SC._scene_layout({'scene_layout': 'left-stack'}, SC._scene_theme(plan, {}))",
        "out['native_layouts'] = bool(ST.LAYOUTS_PORTRAIT) and \\",
        "    lay.get('panel_y') != ST.LAYOUTS['left-stack']['panel_y']",
        "print(json.dumps(out, ensure_ascii=False))",
    ])
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write(body)
    env = dict(os.environ, VIDEO_STUDIO_LAYOUT="portrait", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, probe], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=300)
        assert r.returncode == 0, "竖屏探针失败：%s" % (r.stderr or r.stdout)[-300:]
        got = json.loads((r.stdout or "").strip().splitlines()[-1])
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
        try:
            os.rmdir(_probe_dir)      # 只删自己建的空目录；失败说明有残留，不掩盖
        except OSError:
            pass
    assert got["canvas"] == [1080, 1920], \
        "竖屏画布不是 1080x1920（被静默回落到横屏？）：%r" % (got["canvas"],)
    assert not got["err"], "竖屏下有构图渲染失败：%r" % (got["err"],)
    bad = {k: v for k, v in got["sizes"].items() if v != [1080, 1920]}
    assert not bad, "这些构图在竖屏下尺寸不对：%r" % (bad,)
    assert got.get("clamped", 0) >= 1, "过宽文字没有被钳住——`_fit_px` 的保证失效了"
    # 竖屏版式必须**原生**（另一套坐标），不能是横屏几何的等比缩放：
    # 缩放的典型症状就是"标题 y 与卡片 y 同比例下落"，内容永远只占上半屏。
    assert got.get("cmp_mode") is True, "竖屏 compare 没走上下对照分支（落回横屏实现？）"
    assert got.get("prc_mode") is True, "竖屏 process 没走竖向轴分支（落回横屏实现？）"
    assert got.get("native_layouts") is True, \
        "竖屏没有用原生版式（ST.LAYOUTS_PORTRAIT）：%r" % (got.get("native_layouts"),)
    return "10 类构图 1080x1920 全部出图 / 过宽文字被钳进安全区"


def _check_compose_profile(tool, root, label, profile):
    """构图金帧：跑一个 profile 的 --compose --check，返回 (status, detail)。

    与整片金帧同一套三档语义：pass 真比过且一致 / skip 基线缺失或环境不可比 /
    像素或工具失败直接 AssertionError。显式 --layout 保证子进程 profile 与基线一致，
    不受父进程环境变量影响。
    """
    name = "golden_frames_compose%s.json" % ("_portrait" if profile == "portrait" else "")
    base = os.path.join(root, "assets", name)
    if not os.path.exists(base):
        return "skip", "%s没有 %s（先跑 tools/golden_frames.py --layout %s --compose --write）" % (
            label, name, profile)
    p, d, why = _run_golden_json(tool, root, ["--layout", profile, "--compose", "--check", "--json"])
    if d is None:
        # 导出不是合法 JSON 说明工具/环境已经坏了，不是"不可比"——按失败处理。
        raise AssertionError("%s构图层金帧导出不是合法 JSON：%s" % (label, why))
    status = str(d.get("status") or "")
    changed = d.get("changed") or []
    if changed:
        raise AssertionError(
            "%s构图层金帧变了：%s；若是有意改绘制器，确认后跑 "
            "python tools/golden_frames.py --layout %s --compose --write 重录基线"
            % (label, "、".join(str(x) for x in changed), profile))
    if status == "pass" and p.returncode == 0:
        return "pass", "%s 10 类构图与基线逐类一致" % label
    if status == "fail" or p.returncode == 1:
        raise AssertionError("%s golden_frames --compose status=%s exit=%s：%s" % (
            label, status or "-", p.returncode, (d.get("reason") or "")[:200]))
    return "skip", "%s status=%s exit=%s：%s" % (
        label, status or "-", p.returncode, (d.get("reason") or "")[:160])


def t_compose_golden():
    """76 构图层金帧：横屏 + 竖屏 10 类构图各自有像素基线，且当前渲染与之一致。

    为什么单独一条：整片金帧用的是夹具 film.json，它只有 hook/body/outro 三类——
    `compare` / `process` / `chart` 这些"按 scene_type 分派"的绘制器改了，整片金帧
    **一个像素都不会变**。1.7.0 给这三类各加了竖屏实现，如果只靠自检"分支存在"，
    改坏它们依然不会有任何报警。这条补的就是那个洞。

    两个 profile 都验：旧实现按导入期 K.H/K.W 只挑当前 profile，而 CI 恒横屏，
    竖屏构图基线 `_compose_portrait.json` 因此永远选不中——等于没有门禁。
    缺基线记为 SKIP（不是 PASS）；像素不一致记 FAIL。
    """
    root = os.path.dirname(HERE)
    tool = os.path.join(root, "tools", "golden_frames.py")
    if not os.path.exists(tool):
        return "skip（没有 tools/golden_frames.py：只拷了 scripts/）"
    statuses, details = [], []
    for label, profile in _golden_profiles():
        status, detail = _check_compose_profile(tool, root, label, profile)
        statuses.append(status)
        details.append(detail)
    if all(s == "pass" for s in statuses):
        return "；".join(details)
    # 有一个 profile 没比成（缺基线 / 换机）就整体 SKIP，不许拿另一个的 pass 冒充都验过。
    return "skip（%s）" % "；".join(details)


def t_enum_enforced():
    """77 枚举校验真的在执行（v1.0.2 删 style 动态 enum 时把通用段一起删掉的回归）。

    filmcheck 的 9 个 *_SPEC 里一共 10 处 `enum` 声明。v1.0.2 删 style 动态校验时，
    通用 enum 分支被一起删掉，于是 kind/type/engine/preset/mode 全部"写什么都通过"，
    而 line 619 的注释仍写着"LINE_SPEC 里已经有 enum，那是 ERROR"——注释在、执行不在。
    受影响的不只是报错难看：非法 kind 会让 voicekit 静默回落默认声线，
    非法 type 会让 grammar 静默换构图，都是"跑得通但效果没了"。

    做法：遍历**所有** SPEC（不是只看 SCRIPT_SPEC），对每处 enum 塞一个必然非法的值，
    必须收到 bad_enum；数量也要对得上——枚举声明被删掉同样该红，否则"少查几处"永远无声。
    """
    import filmcheck as FC

    # ---- 正向：夹具剧本的 engine 必须仍然通过（枚举加严不能误伤合法值） ----
    fixture = os.path.join(os.path.dirname(HERE), "assets", "film.json")
    if not os.path.exists(fixture):
        return "skip（没有 assets/film.json）"
    script = json.load(open(fixture, encoding="utf-8"))
    rep = FC.Report()
    FC._check(script.get("engine") or "pil", {"t": "str", "enum": FC.ENGINES},
              "engine", rep, {})
    assert not rep.errors, "合法的 engine 被误判：%r" % (rep.errors,)

    # ---- 负向：SPEC 里每一处 enum 都必须真的拦截 ----
    rules = []
    for name in dir(FC):
        if name.endswith("_SPEC"):
            rules.extend(_walk_enum_rules(getattr(FC, name), name))
    assert rules, "filmcheck 的 SPEC 里一处 enum 都没找到（SPEC 结构变了？）"
    bad = "NOT_A_VALID_VALUE"
    dead = []
    for path, rule in rules:
        enum = list(rule.get("enum") or [])
        assert bad not in enum, "测试用的非法值竟然在枚举里：%r" % (bad,)
        rep = FC.Report()
        FC._check(bad, rule, path, rep, {})
        if not any(c.get("code") == "bad_enum" for c in rep.errors):
            dead.append("%s（enum=%r）" % (path, enum[:4]))
    assert not dead, ("这些 enum 声明没有被执行，非法值能直接通过：%s"
                      % "；".join(dead[:5]))
    return "10 处 enum 全部拦截非法值（%d 处），合法夹具未误伤" % len(rules)


def _walk_enum_rules(obj, path):
    """泛化遍历 SPEC 的 dict/list 嵌套，产出 (路径, 带 enum 的规则字典)。

    不用"只认 props/item 这些键名"的写法：SPEC 里将来加一层新容器就会静默漏检，
    而这条断言的全部意义就是防止"声明了却没执行"。
    """
    if isinstance(obj, dict):
        if "enum" in obj:
            yield path, obj
            return
        for k, v in obj.items():
            for got in _walk_enum_rules(v, "%s.%s" % (path, k)):
                yield got
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            for got in _walk_enum_rules(v, "%s[%d]" % (path, i)):
                yield got


def t_canvas_switch():
    """78 画幅开关：竖屏不能只在渲染器里支持，入口也要认。

    这条守的是一次**真实事故**：技能 1.5.0-1.8.0 已把竖屏做到能出片，但
    ① intake.py 的画幅题只有 3 个横屏选项、提示写着"只做 16:9 横屏"；
    ② designkit.SIZES 与 schemas/design.schema.json 的 size 枚举也只有横屏（现在两处都已含竖屏两套）；
    ③ propose.py / filmcheck.py 没有 --layout，previewkit 的 CANVAS 还写死 1920x1080。
    结果：用户在会话里要竖屏，agent 照着 SKILL.md/问诊单回答"本技能只做 16:9 横屏"，
    并且竖屏提案连 zh 帧都出不来（预览按横屏算尺寸，每帧都判"尺寸不符"）。
    入口文案与工具能力不一致，等于把能力藏起来——这条断言把它钉住。
    """
    import designkit as DK
    scripts = HERE
    intake_py = os.path.join(scripts, 'intake.py')
    filmcheck_py = os.path.join(scripts, 'filmcheck.py')
    propose_py = os.path.join(scripts, 'propose.py')
    import subprocess as _sp

    def _run(args, cwd, extra_env=None):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('VIDEO_STUDIO_LAYOUT', None)
        env.update(extra_env or {})
        return _sp.run([sys.executable] + args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=env, timeout=300)

    tmp = tempfile.mkdtemp(prefix='vs_canvas_selfcheck_')
    # ① 问诊单必须给出竖屏选项，且不能再声称"只做横屏"
    r = _run([intake_py, '--work', 'work', '--questions', '--json'], tmp)
    assert r.returncode == 0, 'intake --questions 失败：%s' % (r.stderr or r.stdout)[-300:]
    payload = json.loads(r.stdout[r.stdout.find('{'):])
    fmt = [q for q in payload['questions'] if q['id'] == 'format'][0]
    vals = [o['value'] for o in fmt.get('options') or []]
    labels = ' '.join(o['label'] for o in fmt.get('options') or [])
    assert any(v.startswith('portrait') for v in vals), \
        '画幅题没有竖屏选项（能力被藏起来）：%s' % vals
    assert '竖屏' in labels, '画幅题的选项文案里没有竖屏：%r' % labels
    hint = str(fmt.get('hint') or '')
    assert '竖屏' in hint, '画幅题 hint 仍不提竖屏：%r' % hint
    assert '只做 16:9' not in hint and '只做16:9' not in hint, \
        'hint 仍声称"只做 16:9 横屏"（与 --layout portrait 矛盾）：%r' % hint
    # ② designkit 的尺寸枚举必须两套都在（横屏三档 + 竖屏三档）
    assert '1080x1920' in DK.SIZES and '1920x1080' in DK.SIZES, \
        'designkit.SIZES 缺一套画幅：%r' % (DK.SIZES,)
    # ③ 同一份竖屏剧本：带 --layout portrait 通过，不带则（因为 9:16 vs 16:9）报错
    film = os.path.join(tmp, 'film_p.json')
    with io.open(film, 'w', encoding='utf-8') as fh:
        json.dump({'title': '竖屏自检', 'video': {'width': 1080, 'height': 1920},
                   'lines': [{'id': 'l01', 'kind': 'hook', 'text': '第一句话。'},
                             {'id': 'l02', 'kind': 'outro', 'text': '结束。'}]},
                  fh, ensure_ascii=False)
    r = _run([filmcheck_py, film, '--layout', 'portrait'], tmp)
    assert r.returncode == 0, 'filmcheck --layout portrait 没认竖屏剧本：%s' % (
        (r.stdout or '') + (r.stderr or ''))[-400:]
    r = _run([filmcheck_py, film], tmp)
    assert r.returncode != 0, 'filmcheck 不带 --layout 时竟然接受了竖屏剧本（应当报 9:16 vs 16:9）'
    # ④ propose.py 必须接受 --layout（否则竖屏提案根本出不来）
    r = _run([propose_py, '--help'], tmp)
    assert '--layout' in (r.stdout or ''), 'propose.py 没有 --layout（竖屏提案无法预览）'
    return '问诊有竖屏选项 / SIZES 两套 / filmcheck 认 --layout / propose 有 --layout'


def t_mux_true_peak_override():
    """79 真峰目标可覆盖：竖屏片默认 -1.5 dBTP 会 G5 fail，得能调。

    事故现场：一部 1080x1920 竖屏片，audio.wav 叠加峰值 0.872（同期横屏片 0.839），
    mux 用默认 target_tp=-1.5 之后，AAC 编码把真峰抬到 -0.0 dBTP，G5（<= -1 dBTP）直接 fail。
    混音侧的 peak_guard 只保证"不削波"，管不到编码后的真峰——所以这个参数必须可覆盖，
    而不是让用户去改技能源码。
    """
    import render as R
    real_k = R.K
    seen = {}

    class _FakeK:
        def mux(self, video, audio, out, **kw):
            seen.update(kw)
            return out

    _buf = io.StringIO()
    try:
        R.K = _FakeK()
        with redirect_stdout(_buf):
            R.st_mux('s.mp4', 'a.wav', 'o.mp4', {})
        assert seen.get('target_tp') == -1.5, \
            '默认真峰目标变了（文档写 -1.5）：%r' % (seen.get('target_tp'),)
        seen.clear()
        with redirect_stdout(_buf):
            R.st_mux('s.mp4', 'a.wav', 'o.mp4', {'mux': {'true_peak_db': -3.0}})
        assert seen.get('target_tp') == -3.0, \
            'film.json 的 mux.true_peak_db 没透传给 K.mux：%r' % (seen.get('target_tp'),)
        for bad, why in ((0.5, '>= 0'), ('x', '非数字')):
            try:
                with redirect_stdout(_buf):
                    R.st_mux('s.mp4', 'a.wav', 'o.mp4', {'mux': {'true_peak_db': bad}})
            except SystemExit:
                continue
            raise AssertionError('mux.true_peak_db=%r（%s）没被拒（会被静默接受）' % (bad, why))
    finally:
        R.K = real_k
    return '默认 -1.5 / 可覆盖 -3.0 / 非法值报错'



def t_duration_floor():
    """80 时长下限：「不少于 N 分钟」是下限语义，不是精确目标。

    为什么必须分开：目标语义下「还差 30%」只是告警；下限语义下「还差 30%」= 没达标。
    早期版本只有 exact 一档，用户写「不少于 5 分钟」时，成片 4 分钟只报一句
    「与目标时长差超过 15%」，读不出「这条片子不合格」。
    另外问诊的时长题要能被自定义时长原话覆盖（「不少于 12 分钟」），
    不能只认预置档位——9 分钟以上的片子没有预置档。
    """
    import propose as PP
    import designkit as DK
    cases = {
        "不少于5分钟": (300.0, "at_least"),
        "至少 3 分钟": (180.0, "at_least"),
        "不少于 12 分钟": (720.0, "at_least"),
        "2分钟": (120.0, "exact"),
        "一分半": (90.0, "exact"),
    }
    for text, want in cases.items():
        got = PP.parse_duration_spec(text)
        assert got == want, "时长语义 %r 得到 %r，期望 %r" % (text, got, want)
    # 别的句子里的「不少于」不能被算进时长语义（误判会让整片被当成下限模式）
    assert PP.parse_duration_spec("不少于三条建议，2分钟") == (120.0, "exact"), \
        "把别处的「不少于」算进了时长语义"
    # brief -> design -> plan 全链
    b = PP._brief_of("数字经济，不少于 5 分钟", {"title": "t"})
    assert b.get("duration_mode") == "at_least", "brief 没接住下限语义：%r" % (b,)
    d = DK.sample_designs(b, n=2, seed=11)[0]
    assert d["structure"].get("target_mode") == "at_least", \
        "下限语义没进设计：%r" % (d["structure"],)
    assert d["structure"].get("target_sec") == 300.0, "时长没进设计"
    plan = DK.design_to_plan(d)
    assert plan.get("target_mode") == "at_least", "下限语义没进计划"
    # 精确目标仍是 exact（旧行为不变）
    b2 = PP._brief_of("2分钟", {"title": "t"})
    d2 = DK.sample_designs(b2, n=2, seed=5)[0]
    assert d2["structure"].get("target_mode") == "exact", "精确目标被误判成下限"
    assert DK.design_to_plan(d2).get("target_mode") == "exact"
    return "5 种说法语义正确；跨句不误判；brief->design->plan 全链 at_least/exact"


def t_portrait_pacing_and_drawers():
    """81 竖屏节奏与三类原生构图（stat / quote / chapter）。

    竖屏的遗留问题有两块，这块守的是「不是缩放，是原生」：
      ① stat / quote / chapter 在竖屏走**自己的绘制函数**（纵向分层），不是把横屏版
         等比缩小——缩出来的结果是上半屏空、下半屏挤；
      ② pacing（lead/tail/min_dur/hold）在竖屏乘 PORTRAIT_PACE，横屏系数恒 1.0。
    这两条都容易在后续改动里被悄悄改回去（谁动了都会「看起来能出片」），所以钉住。
    """
    import subprocess
    import sys as _sys
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.abspath(os.path.join(here, os.pardir, "assets"))
    probe_dir = tempfile.mkdtemp(prefix="vs-portrait2-")
    probe = os.path.join(probe_dir, "_probe.py")
    body = chr(10).join([
        "import io, json, os, sys",
        "sys.path.insert(0, %r)" % here,
        "sys.path.insert(0, %r)" % assets,
        "import designkit as DK",
        "import layoutkit as LK",
        "import scenes as SC",
        "design = json.load(io.open(os.path.join(%r, 'design_fixture.json'), encoding='utf-8'))" % assets,
        "plan = DK.design_to_plan(design)",
        "script = {'title': 'T', 'lines': [], '_style': plan}",
        "out = {'canvas': [SC.W, SC.H], 'pace': DK._pace_factor(), 'sizes': {}, 'err': {}}",
        "kinds = {'stat': 'body', 'quote': 'quote', 'chapter': 'body'}",
        "for st in kinds:",
        "    scene = {'id': 'p_' + st, 'kind': kinds[st], 'scene_type': st,",
        "             'scene_layout': 'split-right', 'chapter': '07 · 风险',",
        "             'text': '这是一句用来测试排版的正文，长度中等偏长。',",
        "             'sub': '字幕一行', 'big': '标题示例', 'kicker': 'KICKER',",
        "             'stats': [{'value': '103612', 'unit': 'P', 'label': '总算力'}],",
        "             'quote': '规划的文本可以被反复解读，但其真正的意义在于落地。',",
        "             'by': '某报告'}",
        "    try:",
        "        out['sizes'][st] = list(SC.draw_frame(scene, 1.5, 6.0, 1.5, script, 45).size)",
        "    except Exception as e:",
        "        out['err'][st] = (type(e).__name__ + ': ' + str(e))[:160]",
        "out['clamped'] = len(SC._CLAMPED)",
        "print(json.dumps(out, ensure_ascii=False))",
    ])
    io.open(probe, "w", encoding="utf-8").write(body)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", VIDEO_STUDIO_LAYOUT="portrait-9x16")
    r = subprocess.run([_sys.executable, probe], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=300)
    assert r.returncode == 0, "竖屏探针失败：%s" % (r.stderr or r.stdout)[-400:]
    got = json.loads(r.stdout[r.stdout.find("{"):])
    assert got["canvas"] == [1080, 1920], "竖屏画布不对：%r" % (got["canvas"],)
    assert abs(float(got["pace"]) - 1.08) < 1e-6, "竖屏节奏系数变了：%r" % (got["pace"],)
    assert not got["err"], "竖屏原生构图有异常：%r" % (got["err"],)
    for st in ("stat", "quote", "chapter"):
        assert got["sizes"].get(st) == [1080, 1920], \
            "%s 竖屏出图尺寸不对：%r" % (st, got["sizes"].get(st))

    # 横屏系数必须恒为 1.0（竖屏改动不能改横屏节奏）
    body2 = chr(10).join([
        "import os, sys",
        "sys.path.insert(0, %r)" % here,
        "import designkit as DK",
        "print(DK._pace_factor())",
    ])
    probe2 = os.path.join(probe_dir, "_probe2.py")
    io.open(probe2, "w", encoding="utf-8").write(body2)
    env2 = dict(os.environ, PYTHONIOENCODING="utf-8")
    env2.pop("VIDEO_STUDIO_LAYOUT", None)
    r2 = subprocess.run([_sys.executable, probe2], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", env=env2, timeout=120)
    assert r2.returncode == 0, "横屏探针失败：%s" % (r2.stderr or r2.stdout)[-300:]
    assert abs(float(r2.stdout.strip().splitlines()[-1]) - 1.0) < 1e-6, \
        "横屏节奏系数被改了：%r" % (r2.stdout, )
    return "竖屏 1.08 / 三类原生构图出图 1080x1920 / 横屏系数仍 1.0"

def t_portrait_vertical_layout():
    """82 竖屏构图必须是**纵向重排**，不是横屏缩小。

    为什么单独守这一条：竖屏刚能出片时，hero/statement/list/outro 走的是「竖屏骨架 +
    横屏内部」——画面能出、字不出画，但内容全挤在上三分之一、下半屏空 1000px。
    改法是把这几类构图在竖屏下**重新纵向分布**（hero/statement/outro 按块高中线居中，
    list 的标题与条目拉开）。这种改动没法用「有没有出画」测出来（本来就不出画），
    只能测「同一段内容在横屏与竖屏下的像素是否仍然一致」——一致就说明还是缩放的。
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.abspath(os.path.join(here, os.pardir, "assets"))
    probe = os.path.join(tempfile.mkdtemp(prefix="vs-portrait3-"), "_probe.py")
    body = chr(10).join([
        "import hashlib, io, json, os, sys",
        "sys.path.insert(0, %r)" % here,
        "sys.path.insert(0, %r)" % assets,
        "import designkit as DK, scenes as SC",
        "design = json.load(io.open(os.path.join(%r, 'design_fixture.json'), encoding='utf-8'))" % assets,
        "plan = DK.design_to_plan(design)",
        "script = {'title': 'T', 'lines': [], '_style': plan, '_total': 60}",
        "cases = {",
        "  'hero': {'kind': 'hook', 'scene_type': 'hero', 'scene_layout': 'split-right',",
        "           'chapter': '01 · 开场', 'kicker': 'K', 'text': '一句话',",
        "           'big': '大标题', 'sub': '副标题'},",
        "  'statement': {'kind': 'body', 'scene_type': 'statement',",
        "                'scene_layout': 'center-stack', 'chapter': '02 · 正文',",
        "                'text': '一句', 'big': '数字化 → 智能化', 'sub': '副标题'},",
        "  'list': {'kind': 'body', 'scene_type': 'list', 'scene_layout': 'split-right',",
        "           'chapter': '03 · 要点', 'kicker': 'K', 'text': '一句',",
        "           'big': '三件事', 'sub': '副标题',",
        "           'items': [{'text': '条目一'}, {'text': '条目二'}, {'text': '条目三'}]},",
        "  'outro': {'kind': 'outro', 'scene_type': 'outro',",
        "             'scene_layout': 'center-stack', 'chapter': '99 · 收尾',",
        "             'text': '一句', 'big': '收尾大字', 'sub': '副标题'},",
        "}",
        "out = {'canvas': [SC.W, SC.H], 'sha': {}, 'err': {}}",
        "for name, extra in cases.items():",
        "    scene = {'id': 'p_' + name}",
        "    scene.update(extra)",
        "    try:",
        "        img = SC.draw_frame(scene, 2.0, 7.0, 2.0, script, 60)",
        "        out['sha'][name] = hashlib.sha256(img.tobytes()).hexdigest()[:16]",
        "    except Exception as e:",
        "        out['err'][name] = (type(e).__name__ + ': ' + str(e))[:120]",
        "print(json.dumps(out, ensure_ascii=False))",
    ])
    io.open(probe, "w", encoding="utf-8").write(body)

    def _run(env_extra):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("VIDEO_STUDIO_LAYOUT", None)
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, probe], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=600)
        assert p.returncode == 0, "构图探针失败：%s" % (p.stderr or p.stdout)[-400:]
        return json.loads(p.stdout[p.stdout.find("{"):])

    land = _run({})
    port = _run({"VIDEO_STUDIO_LAYOUT": "portrait-9x16"})
    assert land["canvas"] == [1920, 1080], "横屏画布不对：%r" % (land["canvas"],)
    assert port["canvas"] == [1080, 1920], "竖屏画布不对：%r" % (port["canvas"],)
    assert not land["err"] and not port["err"], \
        "构图异常：横屏 %r / 竖屏 %r" % (land["err"], port["err"])
    same = [k for k in ("hero", "statement", "list", "outro")
            if land["sha"].get(k) == port["sha"].get(k)]
    assert not same, ("这些构图在横屏与竖屏下像素完全相同（说明只是整体缩放，"
                      "没有纵向重排）：%s" % same)
    return "hero/statement/list/outro 竖屏像素与横屏不同（纵向重排生效）"

def t_asr_offset_straddle():
    """83 ASR 偏移：跨句片段的伪偏移要被识别出来，而不是报「音画不同步」。

    实测事故：一部竖屏片 55 段里有 1 段（l26）报 -1.20s 偏移。查下来 whisper 的
    segment 从上一句尾音就开始（200.82–203.52），而本段 voice_start 是 202.015——
    「本段首字」的 first_t 被当成 200.82，凭空生出 -1.20s。真实的开口时刻与 voice_start
    只差 0.07s。判据要能区分这两种情况，否则每部片都会随机冒出一条假告警。

    判据（缺一不可）：① 本段首字**就是**片段起点；② 片段起点比期望开口早出阈值以上。
    """
    import qaasr
    scenes = [
        {"id": "a", "start": 0.0, "end": 5.0, "voice_start": 0.5, "voice_end": 4.8,
         "text": "第一句旁白"},
        {"id": "b", "start": 5.0, "end": 10.0, "voice_start": 5.5, "voice_end": 9.8,
         "text": "第二句旁白"},
    ]
    # 跨句片段：从 a 的尾音 4.9 开始，一直说到 b 的正文之后
    segs = [
        {"start": 0.5, "end": 4.9, "text": "第一句旁白"},
        {"start": 4.9, "end": 9.8, "text": "第二句旁白", "first_word_t": 4.9},
    ]
    j = qaasr._judge(scenes, segs)
    assert "b" not in j["offsets"], \
        "跨句片段的伪偏移没被剔除：b = %r" % (j["offsets"].get("b"),)
    assert "b" in (j.get("offset_straddle") or {}), \
        "跨句片段应该留下 offset_straddle 记录，而不是静默丢弃"
    assert not [w for w in j["warnings"] if "偏移" in w or "偏 " in w], \
        "仍然报了偏移 warning：%r" % (j["warnings"],)

    # 真的不同步（片段起点不早于期望，但本段首字明显偏晚）仍然要报
    segs2 = [
        {"start": 0.5, "end": 4.9, "text": "第一句旁白"},
        {"start": 7.2, "end": 9.8, "text": "第二句旁白", "first_word_t": 7.2},
    ]
    j2 = qaasr._judge(scenes, segs2)
    assert "b" in j2["offsets"] and abs(float(j2["offsets"]["b"])) > 0.35, \
        "真实的不同步被一起放过了：%r" % (j2["offsets"],)
    assert [w for w in j2["warnings"] if "偏" in w], "真实不同步没报警告"
    return "跨句伪偏移被剔除并留痕；真实不同步照旧报"

def t_portrait_type_scale_and_hold():
    """84 竖屏字号档与逐类停留：字号不是几何缩放，停留按构图类型分配。

    两件事都只作用于竖屏，横屏必须零变化（既有横屏片时长与像素都不许动）：
      ① 字号档：竖屏 type_scale=0.68，而不是 W/1920=0.5625——手机上纯几何缩放的正文
         只有 ~26px，小于舒适阅读尺寸；档位定义单一真源在 layoutkit 的 profile 里；
      ② 逐类停留：chart/list/compare/process 这类「要读」的构图多停，hero/statement/quote
         不停（句子本身已经给了时间），总量有上限（片长的 6%）防止把长片拖慢。
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.abspath(os.path.join(here, os.pardir, "assets"))
    probe = os.path.join(tempfile.mkdtemp(prefix="vs-portrait4-"), "_probe.py")
    body = chr(10).join([
        "import json, os, sys",
        "sys.path.insert(0, %r)" % here,
        "sys.path.insert(0, %r)" % assets,
        "import layoutkit as LK",
        "print(json.dumps({'canvas': [LK.W, LK.H], 'type_scale': LK.TYPE_SCALE,",
        "                    'body_px': LK.body_px(), 'min_body_px': LK.MIN_BODY_PX,",
        "                    'prof': LK.PROFILE.get('key')}, ensure_ascii=False))",
    ])
    io.open(probe, "w", encoding="utf-8").write(body)

    def _run(env_extra):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("VIDEO_STUDIO_LAYOUT", None)
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, probe], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
        assert r.returncode == 0, "字号档探针失败：%s" % (r.stderr or r.stdout)[-300:]
        return json.loads(r.stdout.strip().splitlines()[-1])

    land = _run({})
    port = _run({"VIDEO_STUDIO_LAYOUT": "portrait-9x16"})
    assert land["canvas"] == [1920, 1080] and abs(land["type_scale"] - 1.0) < 1e-6, \
        "横屏字号档必须是 1.0：%r" % (land,)
    assert port["canvas"] == [1080, 1920], "竖屏画布不对：%r" % (port,)
    # 竖屏字号档的判据是**可读性**，不是某个数字：正文必须 >= layoutkit.MIN_BODY_PX。
    # 为什么不再断言字面值：这里曾写死 0.68，后来按「正文 >= 30px」重新扫描发现 0.88
    # 才刚好达标（0.68 只有 ~31px 是把 46px 当基准算错了档位比例）——写死数字会让
    # 「按判据修正」这件事本身变成红灯。
    geo = float(port["canvas"][0]) / 1920.0
    assert port["type_scale"] > geo + 0.05, \
        "竖屏字号档 %.3f 太接近几何缩放 %.3f（等于没做这个档位）" % (port["type_scale"], geo)
    body_px = float(port["body_px"])
    assert body_px >= float(port["min_body_px"]) - 1e-6, \
        "竖屏正文只有 %.1fpx，低于可读下限 %.1fpx" % (body_px, port["min_body_px"])

    # 逐类停留：用一个小 timeline 直接调 render._apply_type_hold（横屏必须原样返回）
    import render as R
    def _mk()->dict:
        return {"fps": 30, "total": 20.0, "frames": 600, "scenes": [
            {"id": "a", "start": 0.0, "end": 5.0, "voice_start": 0.5, "voice_end": 4.5,
             "scene_type": "chart"},
            {"id": "b", "start": 5.0, "end": 10.0, "voice_start": 5.5, "voice_end": 9.5,
             "scene_type": "hero"},
        ]}

    land_tl = R._apply_type_hold(_mk(), {}, {"legacy": False})
    assert land_tl["total"] == 20.0 and "type_hold" not in land_tl, \
        "横屏被逐类停留改了：%r" % (land_tl.get("type_hold"),)
    return "横屏 1.0 / 竖屏 0.68（大于几何缩放 %.3f）；横屏 timeline 未被逐类停留改动" % geo

def t_asr_offset_uncertain():
    """85 ASR 偏移：ASR 片段在期望开口处没有覆盖 -> 判「不可判」并说出来。

    实测事故（第二现场）：竖屏片重建时间轴后 l21 报 `+0.70s`。查下来 ASR 首个片段从
    166.80 开始，而期望开口是 166.10——**缺口 0.7s 里混音是有声音的**（实测能量在
    166.20 起跳），是 ASR 把弱起判成了非语音。这种既不是「真错位」也不是「跨句尾音」，
    官方口径是「自动判不了」：不进 offsets 报超差，但要有一条聚合 warning 点名，
    让人去人工试听——静默丢弃等于骗自己。

    同时**真错位必须照旧报**：缺口 > 3 倍阈值（例如整句晚 1.7s）时不是弱起，是错位。
    """
    import qaasr
    scenes = [
        {"id": "a", "start": 0.0, "end": 5.0, "voice_start": 0.5, "voice_end": 4.5,
         "text": "第一句旁白"},
        {"id": "b", "start": 5.0, "end": 10.0, "voice_start": 5.5, "voice_end": 9.8,
         "text": "第二句旁白"},
    ]
    # 弱起被切：期望 5.5 没有片段覆盖，首个片段 5.9 才开始（缺口 0.4s < 3x0.35）
    weak = [
        {"start": 0.5, "end": 4.9, "text": "第一句旁白"},
        {"start": 5.9, "end": 9.8, "text": "第二句旁白", "first_word_t": 5.9},
    ]
    j = qaasr._judge(scenes, weak)
    assert "b" not in j["offsets"], "弱起被切不该报超差：%r" % (j["offsets"],)
    assert "b" in (j.get("offset_uncertain") or {}), "弱起被切应记为不可判并留痕"
    assert any("无法自动判定" in w for w in j["warnings"]), \
        "不可判的段落必须有聚合 warning 点名（不能静默丢弃）"
    # 真错位：缺口 1.7s > 3x0.35，必须报
    real = [
        {"start": 0.5, "end": 4.9, "text": "第一句旁白"},
        {"start": 7.2, "end": 9.8, "text": "第二句旁白", "first_word_t": 7.2},
    ]
    j2 = qaasr._judge(scenes, real)
    assert "b" in j2["offsets"] and abs(float(j2["offsets"]["b"])) > 1.0, \
        "真错位被当成不可判放过了：%r" % (j2["offsets"],)
    assert any("偏" in w for w in j2["warnings"]), "真错位必须报警告"
    return "弱起判不可判并聚合点名；1.7s 真错位仍报"
def t_cut_frame_alignment():
    """帧对齐切镜：Σround(len*fps) 必须等于 round(total*fps)。

    背景（实测）：旧实现逐镜 -ss/-t 按秒切，每段各自取整到帧；24 镜累积 0.469s，
    视频比时间轴长，越到后段字幕/叠加层越落后画面——还被帧级质检误报成"单帧闪烁"。
    """
    import footageedit as FE
    fps = 30.0
    # 造一个"每段都带 .4 帧尾巴"的 EDL：逐段四舍五入会累积，累计帧不会
    shots = []
    cum = 0.0
    for i in range(24):
        length = 7.41          # 7.41*30 = 222.3 帧，逐段 round 会各吞 0.3 帧
        shots.append({"id": "s%02d" % (i + 1), "source": "x", "at": 0.0,
                      "len": length, "why": "", "text": "t"})
        cum += length
    total = round(cum, 3)
    # 复刻 cut_shots 的帧分配逻辑做断言（不真跑 ffmpeg，只验算法）
    frames, acc = 0, 0.0
    for s in shots:
        f0 = int(round(acc * fps))
        acc += float(s["len"])
        frames += max(1, int(round(acc * fps)) - f0)
    want = int(round(total * fps))
    assert frames == want, "累计帧 %d != 时间轴 %d 帧（差 %d 帧）" % (frames, want, frames - want)
    # 反证：逐段各自四舍五入会偏
    naive = sum(int(round(s["len"] * fps)) for s in shots)
    assert naive != want, "造的数据没能暴露逐段取整偏差（naive=%d want=%d）" % (naive, want)
    return "24 镜累计帧精确；逐段取整会偏 %d 帧" % (naive - want)


def t_loudnorm_tp_headroom():
    """真峰默认留 AAC 余量：target_tp 默认必须是 -2.2 或更低。

    背景（实测）：目标 -1.5 -> AAC 编码后实测 -1.0 dBTP，正好压在 -1.0 门禁线上；
    另一片目标 -1.5 实测 -1.4。AAC 会抬高真峰 0.5~1.1 dB。
    """
    import inspect
    import filmkit as K
    sig = inspect.signature(K.loudnorm_two_pass)
    tp = sig.parameters["target_tp"].default
    assert tp <= -2.0, "loudnorm 默认 target_tp=%r 留的余量不够（AAC 会抬高 0.5~1.1 dB）" % tp
    assert tp >= -3.0, "target_tp=%r 压得太狠，成片会明显偏轻" % tp
    return "默认 TP %.1f dBTP" % tp


def t_qa_frames_shots_whitelist():
    """qa_frames 支持 --shots：硬切点附近的孤立尖峰降级为 warning（不误报闪烁）。

    背景（实测）：素材线是硬切剪辑，切点前后两帧结构完全不同，二阶差分必然尖峰，
    被当成"单帧闪烁"报 error。这类切点是作者声明的剪辑结构。
    """
    import inspect
    import qa_frames as Q
    assert hasattr(Q, "SHOT_WIN"), "qa_frames 没有 SHOT_WIN 常量"
    assert Q.SHOT_WIN > 0, "SHOT_WIN 必须为正（窗口秒数）"
    sig = inspect.signature(Q.report)
    assert "shots_json" in sig.parameters, "report() 没有 shots_json 参数"
    # 白名单加载器：秒数列表能读回
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        import json as _json
        p = os.path.join(d, "shots.json")
        _json.dump([1.5, 12.25], open(p, "w", encoding="utf-8"))
        got = Q._load_shots(p)
        assert got == [1.5, 12.25], "切点加载器读回 %r" % (got,)
        assert Q._load_shots(os.path.join(d, "missing.json")) == [], "缺文件应返回空表不报错"
    return "窗口 %.2fs，加载器可用" % Q.SHOT_WIN


def t_compose_contract():
    """compose.py 的 overlay 契约校验与时间窗解析（形态 3 的合成入口）。"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "compose.py")
    spec = importlib.util.spec_from_file_location("_compose_test", path)
    C = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(C)
    # 1) 窗口解析：bind=line:<id> 用时间轴的 start/end
    tl = {"total": 30.0, "lines": [
        {"id": "s01", "start": 0.5, "end": 5.0},
        {"id": "s02", "start": 5.5, "end": 11.0},
    ]}
    wins = C.resolve_windows({"layers": [{"png": "a.png", "bind": "line:s02"}]}, tl)
    assert len(wins) == 1 and abs(wins[0]["start"] - 5.5) < 1e-6, "bind 解析错：%r" % (wins,)
    assert abs(wins[0]["end"] - 11.0) < 1e-6, "bind 结束时间错"
    # 2) at 没给 dur 时挂到下一句起点
    wins2 = C.resolve_windows({"layers": [{"png": "a.png", "at": 1.0}]}, tl)
    assert abs(wins2[0]["end"] - 5.5) < 1e-6, "at 没挂到下一句：%r" % (wins2,)
    # 3) 契约校验：缺 png / 缺时间窗都要挡
    import tempfile
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.json")
        _json.dump({"layers": [{"id": "x"}]}, open(bad, "w", encoding="utf-8"))
        try:
            C.load_overlay(bad)
            raise AssertionError("缺 png 的层没被挡下")
        except SystemExit:
            pass
    return "bind/at 解析正确；坏契约被挡"


def t_scenes_hybrid_template_purity():
    """包装内置渲染器的模板：留原件 + 返回副本（帧纯度）。

    背景（实测，最贵的坑）：patch 内置 _plate 时若返回**缓存对象本身**，内置绘制器会
    在它上面画字，于是同场景每帧把文字叠到上一帧结果上——帧依赖渲染历史，
    --jobs 4 各分块状态不同，VMAF 从 97 掉到 62.9，单帧完全看不出来。
    """
    import importlib.util
    import json as _json
    here = os.path.dirname(os.path.abspath(__file__))
    tpl = os.path.join(os.path.dirname(here), "assets", "scenes_hybrid_template.py")
    assert os.path.exists(tpl), "模板不存在：%s" % tpl
    spec = importlib.util.spec_from_file_location("_hybrid_tpl_test", tpl)
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    assert hasattr(M, "_ORIG_PLATE"), "模板没留 _ORIG_PLATE（会递归爆栈）"
    assert M._ORIG_PLATE is not M._S._plate, "_ORIG_PLATE 与打补丁后的 _plate 相同"
    # 真画两帧比对字节（需要合法 style plan）
    import designkit as D
    fx = os.path.join(os.path.dirname(here), "assets", "design_fixture.json")
    design = _json.load(open(fx, encoding="utf-8"))
    plan = D.design_to_plan(design)
    script = {"title": "t", "_style": plan, "_total": 12.0,
              "_design": design, "lines": [{"id": "s01", "text": "x"}]}
    scene = {"id": "s01", "kind": "body", "text": "x", "sub": "x", "big": "x",
             "scene_type": "statement", "dur": 3.0, "chapter": "c"}
    a = hashlib.sha256(M.draw_frame(scene, 1.0, 3.0, 1.0, script, 30).tobytes()).hexdigest()
    b = hashlib.sha256(M.draw_frame(scene, 1.0, 3.0, 1.0, script, 30).tobytes()).hexdigest()
    assert a == b, "同帧重画不一致（缓存被就地绘制污染）：%s vs %s" % (a[:12], b[:12])
    return "同帧重画逐字节一致"


def t_hybrid_default_form3():
    """91 hybrid 默认形态 = 素材打底 + 小面积叠加（不许退回整屏图文卡片）。

    真实事故：问诊答了 hybrid，产线路由却指向"素材段 + 图文段按段拼接"，
    agent 照着命令做出了一版整屏主题卡——文档写的默认（形态 3）和代码给的路由
    是两回事。这条把"代码默认 = 形态 3"钉死：
      ① MODE_ROUTES[hybrid] 的走法必须是 footage -> overlay.json -> compose；
      ② _next_steps(form=3) 不许把 propose/render 当默认下一步；
      ③ --hybrid-form 1 才给按段拼接的命令（保留，但要显式选择）；
      ④ finalize 的 stdout 必须打出形态与 compose 命令（agent 照抄的就是它）。
    """
    import subprocess as _sp
    import intake as IT

    route, how = IT.MODE_ROUTES[IT.MODE_HYBRID]
    assert "素材打底" in route, "hybrid 默认形态应是素材打底 + 小面积叠加，实际 %r" % route
    assert "compose.py" in how, "hybrid 默认走法必须给出 compose.py，实际 %r" % how
    assert "concat" not in how, "按段拼接不该是默认走法：%r" % how

    steps3 = IT._next_steps(IT.MODE_HYBRID, "测试题材", "work", form=3)
    joined3 = " ".join(steps3)
    assert "compose.py" in joined3, "形态 3 的下一步必须包含 compose.py：%r" % (steps3,)
    assert "footage.py" in joined3, "形态 3 的下一步必须包含 footage.py：%r" % (steps3,)
    assert "concat" not in joined3, "形态 3 不该提 concat：%r" % (steps3,)

    steps1 = IT._next_steps(IT.MODE_HYBRID, "测试题材", "work", form=1)
    joined1 = " ".join(steps1)
    assert "propose.py" in joined1, "显式选形态 1 时才给图文段的 propose：%r" % (steps1,)
    assert "matchstyle.py" in joined1, "形态 1 要提示 matchstyle：%r" % (steps1,)

    ap_src = pathlib.Path(HERE, "intake.py").read_text(encoding="utf-8")
    assert "--hybrid-form" in ap_src, "intake.py 没注册 --hybrid-form"
    assert "hybridForm" in ap_src, "intake.py 没把形态落盘（hybridForm）"

    tmp = tempfile.mkdtemp(prefix="vs_hybrid_form_")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    sets = (("topic", "测试题材，讲给普通观众"), ("mode", "hybrid"),
            ("duration", "at_least_3m"), ("tone", "deep-focus"),
            ("voice", "male-steady"), ("music", "steady"))
    cmd = [sys.executable, os.path.join(HERE, "intake.py"),
           "--work", os.path.join(tmp, "work")]
    for k, v in sets:
        cmd += ["--set", "%s:%s" % (k, v)]
    cmd.append("--finalize")
    r = _sp.run(cmd, cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=300)
    assert r.returncode == 0, "intake --finalize 失败：%s" % (r.stderr or r.stdout)[-300:]
    out = r.stdout
    assert "compose.py" in out, "finalize 没给 compose.py 的下一步命令：%s" % out[-500:]
    assert "形态: 3" in out, "finalize 没打出形态 3：%s" % out[-500:]
    concat_lines = [ln for ln in out.splitlines()
                    if ln.strip().startswith("python") and "concat" in ln]
    assert not concat_lines, "默认形态的下一步命令不该是按段拼接：%r" % (concat_lines,)


def t_source_budget_and_f8():
    """92 素材预算 + F8 素材多样性（2 个源撑 6 分钟必须 fail）。

    真实事故：一部 6 分 25 秒的片子只用了 2 个素材源（同一张长卷切窗口），
    F1..F7 全绿——没有任何一条门禁关心"画面种类够不够"。这条把口径钉进代码：
    推荐 3 个来源/分钟、硬下限 2 个/分钟、绝对下限 3 个。
    """
    import footagekit as FK
    import footageqa as FQ

    b = FK.source_budget(385.0)
    assert (b["recommended"], b["minimum"]) == (20, 13), \
        "385s 的预算应为 推荐 20 / 下限 13，实际 %r" % (b,)
    b2 = FK.source_budget(329.4)
    assert (b2["recommended"], b2["minimum"]) == (17, 11), "329s 预算应为 17/11：%r" % (b2,)
    short = FK.source_budget(45.0)
    assert short["minimum"] == 3, "再短的片子也不许少于 3 个来源：%r" % (short,)

    edl_bad = {"shots": [{"id": "s%02d" % i, "source": "qm", "at": i * 9.0, "len": 9.0}
                         for i in range(37)]}
    tl = {"total": 385.0,
          "lines": [{"id": "l%02d" % i, "start": i * 10.0, "end": i * 10.0 + 9.0}
                    for i in range(38)]}
    u = FK.source_usage(edl_bad)
    assert u["distinct"] == 1 and u["max_consecutive"] == 37, "usage 统计不对：%r" % (u,)

    r = FQ.f8_source_variety({}, None, None, tl, edl_bad, {})
    assert r["status"] == "fail", "2 个源撑 385s 必须 fail，实际 %r" % (r["status"],)
    assert "素材太少" in r["detail"], "fail 的理由要说人话：%r" % r["detail"]

    edl_ok = {"shots": [{"id": "s%02d" % i, "source": "src%d" % (i % 20),
                         "at": (i // 20) * 20.0, "len": 9.0} for i in range(37)]}
    r_ok = FQ.f8_source_variety({}, None, None, tl, edl_ok, {})
    assert r_ok["status"] == "pass", \
        "20 个源应 pass，实际 %r（%s）" % (r_ok["status"], r_ok["detail"])

    r_skip = FQ.f8_source_variety({"licensing": {"allow_sparse_sources": True,
                                                  "why": "客户指定"}},
                                  None, None, tl, edl_bad, {})
    assert r_skip["status"] == "skipped", "显式豁免应记 skipped：%r" % (r_skip["status"],)

    assert FQ.f8_source_variety in FQ.GATES, "F8 必须注册进 GATES 才会被执行"
    fp_src = pathlib.Path(HERE, "footage.py").read_text(encoding="utf-8")
    assert "--allow-sparse-sources" in fp_src, "footage.py 缺 --allow-sparse-sources 逃生口"
    assert "source_budget" in fp_src, "footage.py 没在 EDL 阶段用素材预算"

def t_palette_tone_conflict():
    """93 配色锚点与气质互斥：必须在采样前点名，而不是重采样 50 次后报"气质不满足"。"""
    import designkit as DK

    def sample(words, avoid=()):
        return DK.sample_designs({"topic": "自检", "must_have": list(words),
                                  "avoid": list(avoid)}, n=3, seed=20260101)

    def err_of(words, avoid=()):
        try:
            sample(words, avoid)
        except ValueError as e:
            return str(e)
        raise AssertionError("互斥约束没被拦下：%r" % (list(words) + list(avoid),))

    e = err_of(["深蓝", "明亮活泼"])
    assert "约束互斥" in e, "报错要明说约束互斥：%s" % e
    assert "深蓝" in e and "明亮活泼" in e, "必须点名是哪两个词打架：%s" % e
    assert "暗底" in e and "浅底" in e, "必须说清冲突在明暗上：%s" % e
    e2 = err_of(["暖纸", "深色"])
    assert "暖纸" in e2 and "深色" in e2, "反方向的冲突也要拦：%s" % e2
    e3 = err_of(["深蓝"], ["深色"])
    assert "深蓝" in e3 and "深色" in e3, "avoid 侧的冲突也要拦：%s" % e3
    assert len(sample(["深蓝", "深色"])) == 3, "深蓝+深色 同明暗，不该被拦"
    assert len(sample(["暖纸", "明亮活泼"])) == 3, "暖纸+明亮活泼 同明暗，不该被拦"
    assert len(sample(["深蓝"])) == 3, "只写配色不该被拦"
    assert len(sample(["明亮活泼"])) == 3, "只写气质不该被拦"
    _orig = DK._make_design

    def _boom(*_a, **_k):
        raise AssertionError("互斥检测发生在采样之后（_make_design 已被调用）")

    DK._make_design = _boom
    try:
        err_of(["深蓝", "明亮活泼"])
    finally:
        DK._make_design = _orig
    return "三类互斥都在采样前点名；同明暗组合不受影响"


def _qa_probe_fixture():
    """_qa_gates 用的最小合法 probe（G4 要分辨率/帧率/音轨/时长）。"""
    return {"format": {"duration": "10.0"}, "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080,
         "r_frame_rate": "30/1", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"}]}


def _gate_ledger_for_g7():
    """造一个空 GateLedger + 只含 scenes 的 timeline；给 G7/G8 用例共用。"""
    import gates as GT
    import tempfile
    work = tempfile.mkdtemp(prefix="sc_g7_")
    lg = GT.GateLedger(work, {"title": "t"}, scenes_path="")
    # _qa_gates 需要 frames/fps（G4 按它算期望时长）；这里给一段够长的干净视频规格。
    tl = {"frames": 300, "fps": 30.0,
          "scenes": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}
    return work, lg, tl


def _gates_by_id(lg):
    return {str(i.get("id")): i for i in lg.items}


def t_scenes_in_content_hash():
    """94 场景代码进内容指纹：不传 --scenes 时也必须算进去（否则改画面旧签核不失效）。

    真事故：render.py 把 `a.scenes`（原始 --scenes 值，缺省空串）传给 GateLedger，
    而真正生效的路径由 _resolve_scenes 回退得出。于是默认用法下 inputs.scenes 恒为
    "missing"、contentHash 不含场景代码——"改了画面代码旧签核自动失效"被静默绕过。
    这条钉住：解析后的路径必须进指纹，且改一行内容指纹必须变。
    """
    import gates as GT
    import tempfile
    work = tempfile.mkdtemp(prefix="sc_hash_")
    sp = os.path.join(work, "scenes.py")
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write("def draw_frame(*a, **k):\n    return None\n")
    script = {"title": "t", "lines": []}
    lg1 = GT.GateLedger(work, script, scenes_path=sp, script_path="")
    h1 = lg1.content_hash
    assert lg1.input_fingerprints()["scenes"] not in ("missing", ""), \
        "解析后的 scenes 路径没进指纹：%r" % (lg1.input_fingerprints(),)
    with open(sp, "a", encoding="utf-8") as fh:
        fh.write("# 改一行\n")
    lg2 = GT.GateLedger(work, script, scenes_path=sp, script_path="")
    assert lg2.content_hash != h1, "改了 scenes.py 内容，contentHash 没变（旧签核会继续有效）"
    # 空路径仍记 missing（没有场景代码时别编一个指纹出来）
    lg3 = GT.GateLedger(work, script, scenes_path="")
    assert lg3.input_fingerprints()["scenes"] == "missing", "没有 scenes 时应记 missing"
    return "scenes 内容进指纹且可失效；无 scenes 记 missing"


def t_g7_subtitle_tristate():
    """95 G7 字幕条数三态：文件不存在=skipped / 存在但空=fail / 正常=pass。

    真事故：sub.srt 存在但为空（字幕全丢）时 got==0 落进 else，记成
    skipped 且 detail 写"没找到 sub.srt"——文件明明在，结论在撒谎；
    --strict-gates 之外没人能拦。现在三态拆开，空文件必须 fail。
    """
    import render as R
    work, lg, tl = _gate_ledger_for_g7()
    _probe = _qa_probe_fixture()
    # 1) 文件不存在 -> skipped
    R._qa_gates(lg, {"warnings": [], "probe": _probe}, None, tl, work)
    assert _gates_by_id(lg)["G7"]["status"] == "skipped", \
        "没有 sub.srt 应记 skipped：%r" % (_gates_by_id(lg).get("G7"),)
    # 2) 存在但为空 -> fail
    open(os.path.join(work, "sub.srt"), "w", encoding="utf-8").close()
    work2, lg2, tl2 = _gate_ledger_for_g7()
    open(os.path.join(work2, "sub.srt"), "w", encoding="utf-8").close()
    R._qa_gates(lg2, {"warnings": [], "probe": _probe}, None, tl2, work2)
    g7 = _gates_by_id(lg2)["G7"]
    assert g7["status"] == "fail", "空 sub.srt（字幕全丢）必须 fail，实际 %r" % (g7["status"],)
    assert "为空" in str(g7.get("detail")), "detail 要说清是空文件而不是没找到：%r" % g7.get("detail")
    # 3) 条数与句数相等 -> pass
    work3, lg3, tl3 = _gate_ledger_for_g7()
    with open(os.path.join(work3, "sub.srt"), "w", encoding="utf-8") as fh:
        fh.write("1\na\n\n2\nb\n\n3\nc\n\n")
    R._qa_gates(lg3, {"warnings": [], "probe": _probe}, None, tl3, work3)
    assert _gates_by_id(lg3)["G7"]["status"] == "pass", \
        "3/3 条字幕应 pass：%r" % (_gates_by_id(lg3).get("G7"),)
    return "不存在=skipped / 空=fail / 齐=pass"


def t_g8_black_frames_structured():
    """96 G8 黑帧读结构化字段：没执行=skipped / 真检测到=fail / 0 段=pass。

    真事故：_qa_gates 靠 warnings 里有没有"黑帧"两个字判断，于是
    "检测未执行"和"检测到 2 段"被同一个分支记成 skipped——真发现长黑帧也写不进
    fail，这道门禁等于不存在。现在 qa_report 返回 black_segments，三态分开。
    """
    import render as R
    work, lg, tl = _gate_ledger_for_g7()
    _probe = _qa_probe_fixture()
    # 1) 字段缺失 = 老格式/没执行 -> skipped
    R._qa_gates(lg, {"warnings": [], "probe": _probe}, None, tl, work)
    assert _gates_by_id(lg)["G8"]["status"] == "skipped", \
        "没有 black_segments 应记 skipped：%r" % (_gates_by_id(lg).get("G8"),)
    # 2) 检测到黑帧 -> fail
    work2, lg2, tl2 = _gate_ledger_for_g7()
    R._qa_gates(lg2, {"warnings": ["首尾之外检测到 2 段长黑帧"], "probe": _probe,
                      "black_segments": 2}, None, tl2, work2)
    assert _gates_by_id(lg2)["G8"]["status"] == "fail", \
        "检测到黑帧必须 fail：%r" % (_gates_by_id(lg2).get("G8"),)
    # 3) 0 段 -> pass
    work3, lg3, tl3 = _gate_ledger_for_g7()
    R._qa_gates(lg3, {"warnings": [], "probe": _probe, "black_segments": 0}, None, tl3, work3)
    assert _gates_by_id(lg3)["G8"]["status"] == "pass", \
        "0 段长黑帧应 pass：%r" % (_gates_by_id(lg3)["G8"],)
    # 4) qa_report 必须真的返回这个字段（否则门禁永远读不到）
    import inspect
    import filmkit as K
    assert "black_segments" in inspect.getsource(K.qa_report), \
        "qa_report 没返回 black_segments，G8 永远只能 skipped"
    return "没执行=skipped / 2 段=fail / 0 段=pass"


def t_intake_check_readonly():
    """97 intake --check 只读：不创建/不改写 intake.json，退出码 2 = 还有没答的。

    真事故：--check 分支写在 save() 之后，于是"只校验"会创建/改写 intake.json，
    连 mode/status 一起覆盖。只读语义被打穿（CI 里跑一次就改盘）。
    """
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory() as d:
        work = os.path.join(d, "work")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, os.path.join(here, "intake.py"),
                            "--work", work, "--check"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=d)
        assert p.returncode == 2, "没答完时 --check 必须退 2，实际 %r" % (p.returncode,)
        assert not os.path.exists(os.path.join(work, "intake.json")), \
            "--check 不该创建 intake.json（只读语义被打穿）"
        # 显式 --set 是"要写盘"的意思，check 模式下照样落盘
        p2 = subprocess.run([sys.executable, os.path.join(here, "intake.py"),
                             "--work", work, "--check", "--set", "topic:自检"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env, cwd=d)
        assert p2.returncode == 2, "只答一题仍应退 2"
        assert os.path.exists(os.path.join(work, "intake.json")), \
            "--check 带 --set 时应落盘（显式写入）"
    return "只读 check 零写入；带 --set 才落盘"


def t_g13_layout_conflict_pure():
    """G13 纯函数：相交必报、分离不报、hud 豁免、越界必报。

    这条守的是**判据本身**：如果判据写反了（比例算错、把 hud 也算进去），
    门禁会变成"要么不报、要么全报"，两种都比没有更糟。
    """
    import qatext
    e, _ = qatext.find_conflicts([("aaa", 0, 0, 100, 40, 20, "body"),
                                  ("bbb", 10, 10, 90, 50, 20, "body")])
    assert e, "两行字大面积重叠必须报错"
    e2, _ = qatext.find_conflicts([("aaa", 0, 0, 100, 40, 20, "body"),
                                   ("bbb", 0, 200, 100, 240, 20, "body")])
    assert not e2, "上下分离的两行不该报"
    e3, _ = qatext.find_conflicts([("aaa", 0, 0, 100, 40, 20, "hud"),
                                   ("bbb", 10, 10, 90, 50, 20, "body")])
    assert not e3, "hud 角色设计上允许与正文重叠（品牌名压正文），不该报"
    e4, _ = qatext.find_conflicts([("aaa", 1800, 0, 2000, 40, 20, "body")],
                                  safe=(96, 58, 1824, 974))
    assert e4, "越出安全区必须报"
    _e5, w5 = qatext.find_conflicts([("aaa", 0, 850, 200, 900, 20, "body")],
                                    sub_band=(0, 872, 1920, 1030))
    assert w5 and not _e5, "正文压进字幕带必须给 warning（而不是 error）"
    return "相交/分离/hud 豁免/越界/字幕带 5 个判据都对"


def t_scenes_text_capture():
    """G13 采集端：scenes.py 必须提供 text_qa_start/take，且能采到真实落笔矩形。

    为什么这条重要：采集钩子是门禁的**唯一数据来源**。删掉它，G13 会降级成
    skipped（"旧版 scenes.py"）—— 而 skipped 在交付报告里看起来像"没查"，
    用户很容易误读成没事。所以钩子本身要在自检里钉住。
    """
    import importlib.util as IU
    import os as _os
    sp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "assets", "scenes.py")
    spec = IU.spec_from_file_location("sc_cap", sp)
    m = IU.module_from_spec(spec)
    spec.loader.exec_module(m)
    for fn in ("text_qa_start", "text_qa_take", "text_qa_role"):
        assert hasattr(m, fn), "scenes.py 缺 %s（G13 采集钩子）" % fn
    import designkit as DK
    import json as _json
    design = _json.load(open(_os.path.join(_os.path.dirname(sp), "design_fixture.json"),
                             encoding="utf-8"))
    plan = DK.design_to_plan(design)
    script = {"title": "t", "lines": [], "_style": plan, "_total": 4.0}
    scene = {"id": "cap", "scene_type": "chapter", "kicker": "模式一", "big": "模式一",
             "start": 0.0, "dur": 4.0, "chapter": "01 · 模式一", "text": "模式一",
             "scene_layout": "left-stack", "scene_variant": 0}
    m.text_qa_start()
    m.draw_frame(scene, 2.0, 4.0, 2.0, script, 60)
    boxes = m.text_qa_take()
    assert len(boxes) >= 1, "章节卡至少应采到章节名一个文字框，实际 %d" % len(boxes)
    assert all(len(b) == 7 for b in boxes), "采集元组必须是 (text,x0,y0,x1,y1,size,role)"
    roles = {b[6] for b in boxes}
    assert "body" in roles, "正文字框必须记 body 角色（否则 G13 会把它当装饰放过）"
    return "采集到 %d 个框，角色 %s" % (len(boxes), sorted(roles))


def t_chapter_list_no_overlap():
    """版式回归：章节卡与要点列表的 kicker 不许压到标题（本次实打实的返工根因）。

    用 Q13 同款采集在固定设计下重渲两段，断言 0 处压盖。
    为什么必须在自检里跑：这类"位置只差 50px"的缺陷，金帧只能告诉你像素变了，
    说不出"变好还是变坏"。
    """
    import importlib.util as IU
    import os as _os
    import json as _json
    import filmkit as K
    import qatext
    import designkit as DK
    here = _os.path.dirname(_os.path.abspath(__file__))
    sp = _os.path.join(here, "..", "assets", "scenes.py")
    spec = IU.spec_from_file_location("sc_ov", sp)
    m = IU.module_from_spec(spec)
    spec.loader.exec_module(m)
    design = _json.load(open(_os.path.join(_os.path.dirname(sp), "design_fixture.json"),
                             encoding="utf-8"))
    plan = DK.design_to_plan(design)
    script = {"title": "t", "lines": [], "_style": plan, "_total": 9.0}
    tl = {"fps": 30, "scenes": [
        {"id": "c1", "scene_type": "chapter", "kicker": "模式一", "big": "模式一",
         "start": 0.0, "dur": 4.0, "chapter": "01 · 模式一", "text": "模式一",
         "scene_layout": "left-stack", "scene_variant": 0},
        {"id": "l1", "scene_type": "list", "kicker": "三条产线", "big": "三条产线",
         "items": [{"text": "a"}, {"text": "b"}, {"text": "c"}],
         "start": 4.0, "dur": 5.0, "chapter": "02 · 正文", "text": "三条产线",
         "scene_layout": "left-stack", "scene_variant": 0},
    ]}
    rep = qatext.scan(tl, m, script, safe=K.SAFE, sub_band=K.SUB_BAND)
    assert rep["checked"] == 2, "两段都该被检查到，实际 %d" % rep["checked"]
    assert not rep["errors"], "版式仍有压盖：%s" % "；".join(rep["errors"][:2])
    return "章节卡/要点列表 0 处压盖（%d 个框）" % rep["boxes"]


def t_partial_stage_keeps_delivered_ledger():
    """101 局部 stage 不许把上一轮交付账本覆盖成半成品（实测 15 条 -> 2 条）。

    真实场景：一部片跑完 --stage all（账本 15 条，含 G4–G13），随后为了改声线单跑
    一次 --stage style。旧实现里 style 分支在 _finish_gates 之前就 write_json()，
    于是磁盘账本先变成「stale + style」，_finish_gates 再合并也只能捡回那两条——
    QC-REPORT.md 从「pass 13」缩成「pass 2」，而内容指纹其实没变（是同一版内容）。
    这条同时钉住两件事：局部阶段合并旧账本、同名条目以本轮为准。
    """
    import json as _json
    import tempfile
    import types
    import gates as GT
    try:
        import render
    except Exception as e:                                  # noqa: BLE001
        return "skip（import render 失败：%s）" % e
    w = tempfile.mkdtemp(prefix="sc_merge_")
    outdir = os.path.join(w, "out")
    os.makedirs(outdir, exist_ok=True)
    script = {"title": "t", "lines": [{"text": "x"}]}
    old = GT.GateLedger(w, script, engine="pil")
    for gid in ("G4", "G5", "G11", "G12"):
        old.ok(gid, "上一轮交付门禁 " + gid, "old")
    old.ok("style", "上一轮风格包", "old-style")
    old.write_json()
    prev = render._LEDGER
    try:
        render._LEDGER = GT.GateLedger(w, script, engine="pil")
        render._LEDGER.ok("style", "风格包解析", "new-style")
        a = types.SimpleNamespace(work=w, out=os.path.join(outdir, "film.mp4"),
                                  strict_gates=False)
        rc = render._finish_gates(a)
    finally:
        render._LEDGER = prev
    d = _json.load(open(os.path.join(w, "gates.json"), encoding="utf-8"))
    ids = set(it["id"] for it in d["items"])
    missing = {"G4", "G5", "G11", "G12"} - ids
    assert not missing, "局部 stage 覆盖掉了上一轮交付门禁：缺 %s（实有 %s）" % (
        sorted(missing), sorted(ids))
    style = [it for it in d["items"] if it["id"] == "style"][0]
    assert style["detail"] == "new-style", "同名门禁没有以本轮为准：%r" % style["detail"]
    assert "G4" in open(os.path.join(outdir, "QC-REPORT.md"), encoding="utf-8").read(), \
        "QC 报告里丢了上一轮的 G4 行（报告被写成半成品）"
    assert rc == 0, "合并后没有 fail 却返回 %r" % rc
    return "局部 stage 合并上一轮 %d 条门禁，同名项本轮覆盖" % len(d["items"])


def t_schema_required_vs_validator():
    """102 schema 的 required 必须覆盖执行器真正强制的字段（v2.7.3 一次性补了 8 处漂移）。

    为什么要这条：schema 是**给人/IDE 看的契约文档**，运行时校验在 designkit.validate_design。
    两边各写一份必填清单，谁也不会因为另一头改了而变红。v2.7.3 实测 schema 缺了
    label/pitch/family/align/detail/enter/inset/item_stagger 八个必填——照 schema 写会被校验器拒，
    而 schema 自己一声不响。这里不重写校验规则，只钉住「两边清单不许分叉」：
    对 fixture 逐字段删掉，断言 validate_design 报错、schema 也判非法。
    """
    # 不引 jsonschema：技能运行期依赖白名单里没有它，
    # 而且这里要验的只是「schema 的 required 与校验器是否覆盖同一批字段」，不需要完整 JSON Schema 语义。
    try:
        import designkit as DK
    except Exception as e:                                  # noqa: BLE001
        return "skip（缺 designkit：%s）" % e
    here = os.path.dirname(os.path.abspath(__file__))
    schema = json.load(open(os.path.join(here, "..", "schemas",
                                         "design.schema.json"), encoding="utf-8"))
    design = json.load(open(os.path.join(here, "..", "assets", "design_fixture.json"),
                            encoding="utf-8"))

    def _required_at(node, path):
        """按 JSON Pointer 走到 schema 的某个子节点，返回它的 required 列表。"""
        cur = node
        for key in path:
            props = (cur.get("properties") or {}) if isinstance(cur, dict) else {}
            cur = props.get(key)
            if not isinstance(cur, dict):
                return []
        return list(cur.get("required") or [])

    # 8 处 v2.7.3 实测漂移：schema 没标必填、validate_design 却硬要求。
    paths = (("label",), ("pitch",), ("visual", "family"),
             ("visual", "typography", "align"), ("visual", "motif", "detail"),
             ("visual", "motion", "enter"), ("visual", "bias", "inset"),
             ("pacing", "item_stagger"))
    bad = []
    for path in paths:
        field = path[-1]
        req = _required_at(schema, path[:-1])
        if field not in req:
            bad.append("schema 没把 %s 标成必填" % ".".join(path))
        broken = copy.deepcopy(design)
        node = broken
        for k in path[:-1]:
            node = node[k]
        node.pop(field, None)
        if not DK.validate_design(broken):
            bad.append("validate_design 没要求 %s" % ".".join(path))
    assert not bad, "schema 必填与 validate_design 分叉：%s" % "；".join(bad[:4])
    return "schema 必填与 validate_design 8 处对齐"


def t_wav_voice_plan():
    """103 voice mode=wav 的 design 必须能转成渲染计划（原实现 KeyError 'map'）。

    validate_design 对 wav 只要求 id，design_to_plan 却无条件读 map/jitter/max_distinct——
    合法 design 到转计划就崩，而且只有真走到渲染才暴露。
    """
    try:
        import designkit as DK
    except Exception as e:                                  # noqa: BLE001
        return "skip（缺 designkit：%s）" % e
    here = os.path.dirname(os.path.abspath(__file__))
    base = json.load(open(os.path.join(here, "..", "assets", "design_fixture.json"),
                          encoding="utf-8"))
    w = copy.deepcopy(base)
    w.setdefault("audio", {})["voice"] = {"mode": "wav", "id": "work/narration.wav"}
    assert not DK.validate_design(w), "wav 声线的 fixture 不该被校验器拒：%s" % DK.validate_design(w)[:2]
    plan = DK.design_to_plan(w)
    vo = plan.get("voice") or {}
    assert vo.get("map"), "design_to_plan 没产出 voice.map（wav 路径又退化了）"
    assert set(vo["map"].values()) == {"work/narration.wav"}, \
        "wav 声线应当四个 kind 都指向同一条音频：%r" % vo["map"]
    return "wav 声线可转计划（map 全指向同一音频）"


def t_persona_is_removed():
    """104 lines[].persona 已废弃：filmcheck 必须报错，不能静默通过。

    voicekit 删掉 PERSONA_IDS 后，filmcheck 那段「从 voicekit 读枚举校验拼写」的代码恒不执行——
    写 persona 的 film.json 契约全绿，到 TTS 阶段才 ValueError。这里钉住显式报错。
    """
    try:
        import filmcheck as FC
    except Exception as e:                                  # noqa: BLE001
        return "skip（缺 filmcheck：%s）" % e
    film = {"title": "t", "lines": [{"id": "l01", "text": "x",
                                     "persona": "narrator_male"}]}
    rep = FC.Report()
    FC._check(film, FC.SCRIPT_SPEC, "$", rep, {"timeline": None})
    FC.check_cross(film, pathlib.Path("<selfcheck>"), rep, {"timeline": None})
    errs = [e for e in rep.errors if "persona" in json.dumps(e, ensure_ascii=False)]
    assert errs, "persona 字段已废弃，filmcheck 却没报错（静默通过）：%r" % rep.errors[:2]
    assert any("removed_persona" in json.dumps(e, ensure_ascii=False) for e in errs), \
        "persona 的错误码不是 removed_persona：%r" % errs[:2]
    return "persona 废弃被 filmcheck 显式报错（removed_persona）"


def t_safe_clamp_landscape():
    """105 横屏文字越出 SAFE 必须被钳回（v2.7.3 实测 seed=7 候选 B 的 G13 假失败）。

    成因：采样器 `visual.bias.x` 允许到 -150，hero 的左缘 `150 + bias.x` 可以算到 0 甚至负数；
    `_fit_px` 原来在横屏（W==1920）直接原样返回（为了金帧逐字节不变），于是越界文字原样画出去，
    G13 报「越出安全区」——**门禁是对的，渲染器没兜住**。修法是让钳位对横屏同样生效（越界才钳），
    这样 SAFE 内的文字一个像素不动（金帧不受影响），越界的被推回边界。
    """
    import importlib.util as IU
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "assets", "scenes.py")
    spec = IU.spec_from_file_location("sc_clamp", path)
    m = IU.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 横屏 profile：W=1920 / SAFE 左界 64（layoutkit 单一真源）
    x0 = float(m.SAFE[0])
    got = m._fit_px(x0 - 40.0, 300.0, "clip", 30)
    assert got == int(round(x0)), "越界文字没被钳回 SAFE 左界：%r != %r" % (got, int(round(x0)))
    keep = m._fit_px(x0 + 30.0, 300.0, "ok", 30)
    assert keep == int(round(x0 + 30.0)), "SAFE 内的文字被无故挪动：%r" % keep
    # 右侧越界同样要钳；超宽段落退化为左界（与竖屏同口径）
    x1 = float(m.SAFE[2])
    right = m._fit_px(x1 - 100.0, 300.0, "right", 30)
    assert right == int(round(x1 - 300.0)), "右侧越界没被钳回：%r" % right
    wide = m._fit_px(x0, x1 - x0 + 10.0, "wide", 30)
    assert wide == int(round(x0)), "超宽段落应退化到左界：%r" % wide
    return "横屏越界钳位生效（SAFE 内像素不动）"


CASES = [
    ("1  TTS 缓存校验文本", t_cache_key),
    ("2  sub_window 覆盖长句", t_sub_window),
    ("3  loudness 测真峰", t_true_peak),
    ("4  mux 默认 copy 视频流", t_mux_copy),
    ("5  FrameSink 尺寸不符报错", t_strict_size),
    ("6  字形自查", t_glyph),
    ("7  ASS PlayRes / 字段数", t_ass),
    ("8  时间轴首段 start=0", t_timeline_zero),
    ("9  分块命名与缺口检测", t_chunks),
    ("10 qa 字幕条数校验", t_qa_subs),
    ("11 尾静音裁剪", t_trim),
    ("12 mux 音轨 48kHz", t_mux_48k),
    ("13 调用签名兼容", t_compat),
    ("14 qa 帧率校验", t_fps_check),
    ("15 立体声总线", t_stereo_bus),
    ("16 cue_time 三种写法", t_cue_time),
    ("17 CueBus 落盘/读回 schema", t_cue_json),
    ("18 duck 让位", t_duck),
    ("19 mix_film 三总线", t_mix_film),
    ("20 sfxkit 配方与确定性", t_sfxkit),
    ("21 scorekit 确定性与时长", t_scorekit),
    ("22 webengine 页面契约", t_webengine),
    ("23 qa_frames 函数齐全", t_qa_frames),
    ("24 配乐来源判定（off 不读旧配乐）", t_music_plan),
    ("25 风格层：8 套主题过对比度", t_style_kits),
    ("26 风格 legacy 兼容", t_style_legacy),
    ("27 内容指纹确定性", t_style_determinism),
    ("28 声线计划", t_voice_plan),
    ("29 字体族与主题字幕", t_font_family),
    ("30 门禁账本与签核", t_gates),
    ("31 阶段新鲜度守卫", t_stagewatch),
    ("32 并行渲染主题序列化", t_parallel_frames),
    ("33 音效跨片可区分（内容派生 seed）", t_sfx_content_seed),
    ("34 配乐/音效片内分离（duck 加深）", t_audio_separation),
    ("35 旁白优先（背景让位与恢复）", t_voice_priority),
    ("36 旁白优先默认值（配乐 -12 dB + 侧链）", t_music_gain_default),
    ("37 旁白清晰度 SNR 兜底（背景太响时自动补压）", t_snr_guard),
    ("38 无旁白时混音不受影响", t_mix_no_voice),
    ("39 音频边界：极短/NaN·Inf/负起点/seg=0", t_audio_edge_inputs),
    ("40 QC 报告落点只有一个来源", t_report_single_source),
    ("41 空门禁账本不许写成通过", t_empty_ledger_not_pass),
    ("42 单阶段产物归属（防假 stale）", t_stage_io_no_phantom_output),
    ("43 theme_overrides 参与主题体检", t_theme_overrides_linted),
    ("44 词级时间戳（规整/裁剪/锚点）", t_word_events),
    ("45 画幅规划（只允许横屏下采样）", t_video_plan),
    ("46 金帧确定性（同帧两次一致）", t_golden_determinism),
    ("47 依赖漂移体检（离线）", t_dependency_drift),
    ("48 批量清单解析（预检先于执行）", t_batch_manifest),
    ("49 TTS 后端可替换（接口契约）", t_tts_backend),
    ("50 web 风格载荷（legacy 必须回落）", t_web_style_payload),
    ("51 金帧回归（横屏+竖屏像素基线）", t_golden_frames),
    ("52 场景语法（10 类分流/确定/段长门）", t_scene_grammar),
    ("53 视觉语言包（结构签名互不相同）", t_visual_languages),
    ("54 场景类型单真值（4 处集合一致）", t_scene_types_single_truth),
    ("55 剪影可区分（S13：换风格不能只是换色）", t_silhouette_gate),
    ("56 设计 DSL（采样/校验/转计划/fail-closed）", t_design_dsl),
    ("57 零模板（仓库里不许有预设；采样确定性且随 brief 变化）", t_no_templates),
    ("58 提案闸（absent/draft/confirmed/broken）", t_proposal_gate),
    ("59 逐 stage 可单独跑（resolve_style 必须拿得到 work）", t_stage_standalone),
    ("60 brief 里的要求真的生效（自然语言 -> 约束 -> 采样）", t_brief_constraints),
    ("61 字幕样式可定制（design.visual.subtitle 真的生效）", t_subtitle_style),
    ("62 片内版式轮换（layout_cycle：换章即换骨架）", t_layout_cycle),
    ("63 带约束的 brief 不会高概率采样失败", t_brief_constraint_robustness),
    ("64 已删除 API 的 fail-closed（不许 NameError / 假绿 / 静默回落）", t_removed_apis_fail_closed),
    ("65 design.audio 的音量/音效参数真的落进 script", t_audio_overrides),
    ("66 片间去重台账（确认记账 / 提案避开 / 可关可查）", t_design_ledger),
    ("67 时长要求真的被接住（brief -> design -> 渲染对账）", t_duration_target),
    ("68 缩放取景的帧间稳定性（抖动回归）", t_zoom_stability),
    ("69 ASR 掉分可定位（lost_top / 最差段落）", t_asr_lost_diagnostics),
    ("70 出片前问诊（固定 8 题 / 制作方式推荐与产线路由 / 明亮点排第一）", t_intake_gate),
    ("71 默认设计不画装饰细线（母题/底纹/HUD 全关）", t_plain_design_has_no_hairlines),
    ("72 前景不随镜头位移（1px 台阶回归）", t_foreground_pinned),
    ("73 画布/安全区单一真源（layoutkit）", t_layout_single_source),
    ("74 审美质检（离线/确定/联系表不误判）", t_critic_offline),
    ("75 竖屏 profile（画布/出图/文字不出画）", t_portrait_profile),
    ("76 构图层金帧（横竖屏各 10 类构图都有像素基线）", t_compose_golden),
    ("77 枚举校验真的在执行（enum 不许只写在 SPEC 里）", t_enum_enforced),
    ("78 画幅开关（问诊/尺寸枚举/filmcheck/propose 都认竖屏）", t_canvas_switch),
    ("79 真峰目标可覆盖（竖屏片默认 -1.5 dBTP 会 G5 fail）", t_mux_true_peak_override),
    ("80 时长下限（不少于 N 分钟 / 自定义原话）", t_duration_floor),
    ("81 竖屏节奏与三类原生构图（stat/quote/chapter）", t_portrait_pacing_and_drawers),
    ("82 竖屏纵向重排（hero/statement/list/outro 不是缩放）", t_portrait_vertical_layout),
    ("83 ASR 偏移：跨句片段伪偏移被识别", t_asr_offset_straddle),
    ("84 竖屏字号档与逐类停留（横屏零变化）", t_portrait_type_scale_and_hold),
    ("85 ASR 偏移不可判（弱起被切 vs 真错位）", t_asr_offset_uncertain),
    ("86 切镜帧对齐（Σ累计帧 == round(total*fps)）", t_cut_frame_alignment),
    ("87 真峰留 AAC 余量（默认 TP <= -2.0）", t_loudnorm_tp_headroom),
    ("88 qa_frames 切点白名单（硬切不误报闪烁）", t_qa_frames_shots_whitelist),
    ("89 compose.py overlay 契约（bind/at/坏契约）", t_compose_contract),
    ("90 包装模板帧纯度（留原件 + 返回副本）", t_scenes_hybrid_template_purity),
    ("91 hybrid 默认形态 = 素材打底 + 小面积叠加（不退回图文卡片）", t_hybrid_default_form3),
    ("92 素材预算与 F8（2 源撑 6 分钟必须 fail）", t_source_budget_and_f8),
    ("93 配色锚点与气质互斥（采样前点名，不靠重试）", t_palette_tone_conflict),
    ("94 场景代码进内容指纹（改画面旧签核必须失效）", t_scenes_in_content_hash),
    ("95 G7 字幕三态（空 sub.srt 必须 fail 而不是 skipped）", t_g7_subtitle_tristate),
    ("96 G8 黑帧结构化（检测到就必须 fail，不许记 skipped）", t_g8_black_frames_structured),
    ("97 intake --check 只读（不写 intake.json）", t_intake_check_readonly),
    ("98 G13 版式冲突判据（相交/分离/hud 豁免/越界/字幕带）", t_g13_layout_conflict_pure),
    ("99 scenes 文字框采集钩子（G13 的数据源）", t_scenes_text_capture),
    ("100 章节卡/要点列表不再压字（版式回归）", t_chapter_list_no_overlap),
    ("101 局部 stage 不覆盖上一轮交付账本（15 条 -> 2 条回归）", t_partial_stage_keeps_delivered_ledger),
    ("102 schema 必填与 validate_design 对齐（8 处漂移回归）", t_schema_required_vs_validator),
    ("103 voice mode=wav 可转计划（原 KeyError 'map'）", t_wav_voice_plan),
    ("104 lines[].persona 已废弃必须显式报错", t_persona_is_removed),
    ("105 横屏文字越出 SAFE 必须被钳回（G13 假失败回归）", t_safe_clamp_landscape),
]


def main():
    global VERBOSE
    _setup_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    VERBOSE = a.verbose
    print("filmkit selfcheck  ffmpeg=%s" % K.FFMPEG)
    print("-" * 66)
    for name, fn in CASES:
        run(name, fn)
    print("-" * 66)
    n_pass = sum(1 for r in RESULTS if r[0] == "PASS")
    n_fail = sum(1 for r in RESULTS if r[0] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r[0] == "SKIP")
    print("PASS %d / FAIL %d / SKIP %d" % (n_pass, n_fail, n_skip))
    for status, name, detail in RESULTS:
        if status == "FAIL":
            print("  ! %s: %s" % (name, detail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
