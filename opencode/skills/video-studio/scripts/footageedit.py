# -*- coding: utf-8 -*-
"""footageedit.py — 素材剪辑生产线的剪辑层。

与零素材线的 render.py 对照看：
  * render.py 的"帧"来自 draw_frame(t, frame)，时间轴由 timeline.json 定；
  * 这里的"帧"来自素材文件，时间轴由**旁白实测时长**反推，镜头长度跟着旁白走。
两条线共同的纪律（一条都不能破）：
  1. 音频先做、画面后做：镜头窗口 = 旁白起点 - lead .. 旁白终点 + tail；
  2. 混音在 numpy 里按采样点叠加，不用 ffmpeg 多输入（Windows 上会非确定性静音）；
  3. 视频与音频总线**严格等长**，否则 -shortest 会悄悄截掉片尾。

只提供函数；命令行入口在 footage.py。画面治理滤镜来自
footagekit.build_treatment_filter，与抽帧预览共用同一份，避免"预览好看、成片不一样"。
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import filmkit      # noqa: E402
import footagekit   # noqa: E402

_run = filmkit._run      # 同目录 filmkit 的子进程封装（capture_output，支持 cwd=）
SR = 48000
DEFAULT_LEAD = 0.35
DEFAULT_TAIL = 0.55


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ============================================================================
# 1. 时间轴：由 TTS 实测时长反推镜头窗口
# ============================================================================

def build_timeline(footage, tts_rows):
    """把实测旁白排成镜头时间轴。

    规则：
      shot_start = start - lead（画面先进，声音后起，听感更稳）
      shot_end   = end + tail + 本句 hold（最后一个字不能贴剪辑点）
      收尾 outro 的留白**并进最后一镜**：预算成独立空档会让视频比音频短，
      assemble 的等长校验会直接拒片（实测差 1.55s 就是这么来的）。
    """
    pacing = footage.get("pacing") or {}
    lead = float(pacing.get("lead", DEFAULT_LEAD))
    tail = float(pacing.get("tail", DEFAULT_TAIL))
    by_id = {str(r["id"]): r for r in tts_rows}
    rows, t = [], 0.0
    for ln in footage["lines"]:
        lid = str(ln["id"])
        dur = float(by_id[lid]["dur"])
        hold = float(ln.get("hold") or pacing.get("hold") or 0.0)
        t += lead
        start = t
        t += dur + hold + tail
        end = start + dur
        rows.append({"id": lid, "text": str(ln["text"]), "dur": round(dur, 3),
                     "start": round(start, 3), "end": round(end, 3),
                     "hold": round(hold, 3),
                     "shot_start": round(start - lead, 3),
                     "shot_end": round(end + tail + hold, 3)})
    outro = float(pacing.get("outro", 2.2))
    if rows and outro > 0:
        rows[-1]["shot_end"] = round(rows[-1]["shot_end"] + outro, 3)
    total = round(rows[-1]["shot_end"], 3) if rows else 0.0
    for r in rows:
        r["shot_len"] = round(r["shot_end"] - r["shot_start"], 3)
    rows[0]["shot_start"] = 0.0 if rows else 0.0
    return {"total": total, "lead": lead, "tail": tail, "outro": outro,
            "lines": rows, "created": footagekit._now()}


# ============================================================================
# 2. EDL：镜头 -> 源文件时间码（可审计、可手改）
# ============================================================================

def make_edl(footage, timeline, sources_info=None):
    """由 lines[].shot 生成 EDL，落盘 work/edl.json（可手改后重跑 cut）。

    两种写法：
      shot: {"source": "film1983", "at": 175.0}          # at = 源文件内绝对秒数
      shot: {"source": "film1983", "at": 175.0, "len": 9.0}
    len 省掉时用时间轴的 shot_len（推荐：改台词后镜头自动跟着变长变短）。
    """
    srcs = footagekit.source_map(footage)
    rows = {r["id"]: r for r in timeline["lines"]}
    shots = []
    for ln in footage["lines"]:
        lid = str(ln["id"])
        row = rows[lid]
        shot = ln.get("shot") or {}
        sid = str(shot.get("source") or "")
        if not sid or sid not in srcs:
            raise SystemExit(
                "lines[%s] 没有可用的 shot.source（可选：%s）。\n"
                "  素材线必须给每一句指定画面；只想用一张图就一路指同一个源。"
                % (lid, ", ".join(sorted(srcs))))
        at = shot.get("at")
        if at is None:
            raise SystemExit("lines[%s].shot 缺 at（源文件内绝对秒数）" % lid)
        # 手填的 at/len 是字符串（"175"）时 float() 可以接受，但写错字（"1分30"）会在
        # 这里抛裸 ValueError：栈里看不出是哪一句，用户只能猜。统一转成可读的报错。
        try:
            at_v = float(at)
        except (TypeError, ValueError):
            raise SystemExit("lines[%s].shot.at 必须是秒数（收到 %r）" % (lid, at)) from None
        if at_v < 0:
            raise SystemExit("lines[%s].shot.at=%s 不能是负数" % (lid, at_v))
        try:
            length = float(shot["len"]) if shot.get("len") is not None else row["shot_len"]
        except (TypeError, ValueError):
            raise SystemExit("lines[%s].shot.len 必须是秒数（收到 %r）"
                             % (lid, shot.get("len"))) from None
        if length <= 0.2:
            raise SystemExit("lines[%s] 的镜头长度 %.2fs 过短（至少 0.3s）" % (lid, length))
        shots.append({"id": lid, "source": sid, "at": round(at_v, 3),
                      "len": round(length, 3), "why": shot.get("why") or "",
                      "text": str(ln["text"])[:60]})
    edl = {"schema": 1, "title": footage.get("title"), "total": timeline["total"],
           "lead": timeline["lead"], "tail": timeline["tail"],
           "shots": shots, "created": footagekit._now()}
    # sources_info 必须传进来才有源时长可比：不传时越界检查会静默"通过"
    # （自检里踩过：at=22 + len=4.4 超过 24s 源，却没报任何问题）。
    return edl, check_edl(edl, footage, sources_info=sources_info)


def check_edl(edl, footage, sources_info=None):
    """EDL 静态检查：越界 / 素材缺失 / 总长与时间轴差太多。返回问题列表（空=通过）。"""
    info = sources_info or {}
    srcs = footagekit.source_map(footage)
    problems = []
    for s in edl["shots"]:
        sid = s["source"]
        if sid not in srcs:
            problems.append("[%s] source=%r 不存在" % (s["id"], sid))
            continue
        rec = info.get(sid) or {}
        dur = rec.get("dur")
        if dur:
            if s["at"] + s["len"] > float(dur) + 0.05:
                problems.append("[%s] 越界：at=%s + len=%s > 源时长 %s（会切到不存在的画面）"
                                % (s["id"], s["at"], s["len"], dur))
            if s["at"] < 0:
                problems.append("[%s] at=%s < 0" % (s["id"], s["at"]))
    want = float(edl.get("total") or 0.0)
    got = sum(s["len"] for s in edl["shots"])
    if want and abs(got - want) > 0.6:
        problems.append("镜头总长 %.2fs 与时间轴 %.2fs 差 %.2fs（>0.6s：画面会被截断或留黑）"
                        % (got, want, abs(got - want)))
    # 帧对齐核对：cut 现在是按累计帧切的，Σround(len*fps) 必须等于 round(total*fps)。
    # 不等就说明 EDL 的 len 与时间轴对不上，音画会随片长累积错位（实测 24 镜差 0.469s）。
    if want:
        fps = float((footage.get("video") or {}).get("fps") or 30)
        cum, frames = 0.0, 0
        for s in edl["shots"]:
            f0 = int(round(cum * fps))
            cum += float(s["len"])
            frames += int(round(cum * fps)) - f0
        want_frames = int(round(want * fps))
        if frames != want_frames:
            problems.append("帧数合计 %d 与时间轴 %d 帧不符（差 %d 帧 / %.3fs）："
                            "音画会随片长累积错位，检查 EDL 的 len 与 timeline.total"
                            % (frames, want_frames, frames - want_frames,
                               (frames - want_frames) / fps))
    return problems


# ============================================================================
# 3. cut：切镜（画面治理 + 音轨分离）
# ============================================================================

def cut_shots(footage, edl, work, *, crf=16, preset="veryfast"):
    """按 EDL 逐镜切出统一画幅的片段，同时抽出该镜的原声音轨。

    单独抽音轨的原因：素材自带的解说/对白是污染源，但里面也有枪炮、脚步、风和水
    这些有用环境声。素材线的做法是把原声整体压到很低（默认约 -18 dB）当环境层，
    而不是整段静音——静音会让画面显得"死"。
    """
    work = Path(work)
    segs = work / "segs"
    segs.mkdir(parents=True, exist_ok=True)
    srcs = footagekit.source_map(footage)
    vf = footagekit.build_treatment_filter(footage.get("treatment") or {},
                                          footage.get("video") or {})
    # 帧对齐切镜：逐镜 -t 取整会在 20+ 镜时累积到半秒级的音画错位
    # （实测 24 镜差 0.469s，表现为后段字幕/叠加层落后画面，还可能被帧级质检误报成闪烁）。
    # 这里按**累计帧**分配每镜帧数，Σframes == round(total*fps)，与时间轴严格一一对应。
    fps = float((footage.get("video") or {}).get("fps") or 30)
    audio_cache = {}

    def _src_has_audio(path):
        """源文件有没有音轨（每个源只探一次）。

        没有音轨的源很常见：图片、--download-sections 只给视频流的分段下载。
        这类源不抽音轨，cut.json 里记 audio=None（表示"本来就没有"，不是"丢了"）；
        探测失败（streams 为空）时按有音轨处理，让下面的退出码检查去报错，
        而不是把"没探到"当"没音轨"静默丢一条环境声。
        """
        key = str(path)
        if key not in audio_cache:
            raw = filmkit.probe(key) or {}
            streams = raw.get("streams") or []
            audio_cache[key] = (any(x.get("codec_type") == "audio" for x in streams)
                                if streams else True)
        return audio_cache[key]

    out = []
    cum = 0.0
    for i, s in enumerate(edl["shots"]):
        src = footagekit.source_path(footage, srcs[s["source"]])
        if not src.exists():
            raise SystemExit("[cut] 素材不存在：%s（source=%s，先跑 --stage fetch）"
                             % (src, s["source"]))
        dst = segs / ("%02d_%s.mp4" % (i, s["id"]))
        wav = segs / ("%02d_%s.wav" % (i, s["id"]))
        f0 = int(round(cum * fps))
        cum += float(s["len"])
        f1 = int(round(cum * fps))
        n = max(1, f1 - f0)                      # 本镜帧数（累计差，不是单段四舍五入）
        span = n / fps                           # 与画面严格等长的秒数
        still = src.suffix.lower() in footagekit.IMAGE_EXT
        log("  [cut] %02d %-8s %-24s at=%7.2f len=%.2f (%d 帧)" %
            (i, s["id"], src.name, s["at"], span, n))
        # 图片源只有一帧：不加 -loop 1 时 -frames:v n 也只输出 1 帧（实测 0.033s），
        # 成片会拼成一堆 1 帧碎片、assemble 的等长校验直接拒片。静帧按 -loop 1 铺满本镜。
        lead = ["-loop", "1"] if still else []
        p = _run([filmkit.FFMPEG, "-v", "error", "-y"] + lead + [
            "-ss", "%.3f" % s["at"], "-i", str(src), "-frames:v", str(n),
            "-r", "%g" % fps, "-vf", vf, "-an",
            "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-pix_fmt", "yuv420p", str(dst)])
        if p.returncode != 0 or not dst.exists():
            raise SystemExit("[cut] 第 %02d 镜（%s）切镜失败（ffmpeg exit=%d）：\n%s\n"
                             "  常见原因：素材比镜头短（at=%.2fs + len=%.2fs 超出源可用画面）"
                             % (i, s["id"], p.returncode,
                                (p.stderr or b"").decode("utf-8", "replace").strip()[-600:],
                                s["at"], span))
        # 源素材比镜头短时 ffmpeg 会"成功"但只给几帧：必须自己量一遍，
        # 否则音画从这一镜起累积错位，最后在 assemble 里被含糊地报成"音视频不一致"。
        try:
            seg_dur = filmkit.media_dur_checked(str(dst), "切出的镜头")
        except RuntimeError as e:
            raise SystemExit("[cut] 第 %02d 镜（%s）读不出时长（素材可能比镜头短）：%s"
                             % (i, s["id"], e)) from None
        if seg_dur < span - 1.5 / fps:
            raise SystemExit("[cut] 第 %02d 镜（%s）只切到 %.3fs，应切 %.3fs："
                             "素材比镜头短（at=%.2fs + len=%.2fs 超出源可用画面）。\n"
                             "  改 EDL 的 at/len，或给这段旁白换更长的素材"
                             % (i, s["id"], seg_dur, span, s["at"], span))
        has_audio = (not still) and _src_has_audio(src)
        if has_audio:
            p = _run([filmkit.FFMPEG, "-v", "error", "-y", "-ss", "%.3f" % s["at"],
                      "-t", "%.4f" % span, "-i", str(src), "-vn",
                      "-af", "aresample=%d" % SR, "-ac", "2", "-ar", str(SR), str(wav)])
            if p.returncode != 0 or not wav.exists():
                raise SystemExit("[cut] 第 %02d 镜（%s）抽原声失败（ffmpeg exit=%d）：%s"
                                 % (i, s["id"], p.returncode,
                                    (p.stderr or b"").decode("utf-8", "replace").strip()[-600:]))
        else:
            log("  [cut] %02d %s 没有音轨（图片源 / 分段下载只给视频流）：本镜环境层跳过"
                % (i, s["id"]))
        out.append({"id": s["id"], "video": str(dst),
                    "audio": str(wav) if has_audio else None,
                    "len": round(span, 4), "frames": n,
                    "at": s["at"], "source": s["source"]})
    return out


# ============================================================================
# 4. 混音：旁白 + 环境层 + 配乐，采样级叠加
# ============================================================================

def decode_audio(path, sr=SR):
    """解成 (N,2) float32；单声道自动复制成双声道，保证总线形状统一。

    解码失败**必须抛错**，不能返回空数组：调用方会把它直接加进总线，
    空数组等于"这一轨整条消失"，而 QC 报告里看不出任何痕迹。
    （2026-11 审查实测：素材路径存在但内容坏时，原实现静默丢轨。）
    """
    import numpy as np
    proc = subprocess.run([filmkit.FFMPEG, "-v", "error", "-i", str(path),
                           "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
                          capture_output=True)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("音频解码失败（%s，ffmpeg rc=%d）：%s"
                           % (path, proc.returncode, (tail[-1] if tail else "无 stderr")[:200]))
    a = np.frombuffer(proc.stdout, dtype=np.float32)
    if len(a) % 2:
        a = a[:-1]
    if a.size == 0:
        raise RuntimeError("音频解码结果为空（%s）：文件存在但没有可解的音轨" % path)
    return a.reshape(-1, 2).copy()


def mix_tracks(tts_rows, timeline, cut_rows, footage, work):
    """三条总线叠加：旁白（voice_gain=1.0）/ 素材原声（env_gain≈0.12）/ 配乐（music_gain≈0.10）。

    总长取 timeline.total 而不是各轨自然长度：视频按时间轴拼，音频必须同长。
    """
    import numpy as np
    audio = footage.get("audio") or {}
    work = Path(work)
    total = float(timeline["total"])
    n = int(round(total * SR))
    bus = np.zeros((n + SR, 2), dtype=np.float32)

    def add(path, at, gain, loop=False, *, required=True, label=""):
        # 缺失的音轨必须炸：静默 return 等于"这条轨整条消失"，而 QC 只看音画等长
        # （等长比的是总线长度，丢一条轨看不出来）。audio=None 表示"这个源本来就没有
        # 音轨"（图片 / 分段下载只给视频流），由调用方显式传 None，不算缺失。
        if path is None or str(path) == "":
            return
        p = Path(path)
        if not p.is_file():
            if not required:
                log("  [mix] WARN 跳过缺失音轨 %s（%s）" % (p, label or "可选轨"))
                return
            raise SystemExit("[mix] 缺音轨 %s（%s）：不会静默丢轨，先补文件或重跑对应阶段"
                             % (p, label or "必需轨"))
        a = decode_audio(p) * float(gain)
        i0 = int(round(max(0.0, at) * SR))
        if loop and a.shape[0]:
            while i0 < bus.shape[0]:
                j = min(bus.shape[0], i0 + a.shape[0])
                bus[i0:j] += a[:j - i0]
                i0 += a.shape[0]
            return
        j = min(bus.shape[0], i0 + a.shape[0])
        if j > i0:
            bus[i0:j] += a[:j - i0]

    rows = {r["id"]: r for r in timeline["lines"]}
    for r in tts_rows:
        add(work / "tts" / (str(r["id"]) + ".mp3"),
            rows[str(r["id"])]["start"], float(audio.get("voice_gain", 1.0)),
            label="旁白 %s（先跑 --stage tts）" % r["id"])
    env_gain = float(audio.get("env_gain", 0.12))
    if env_gain > 0:
        for c in cut_rows:
            add(c.get("audio"), rows[c["id"]]["shot_start"], env_gain,
                label="镜头 %s 的原声（先跑 --stage cut）" % c["id"])
    music_gain = float(audio.get("music_gain", 0.10))
    if music_gain > 0:
        mf = audio.get("music_file")
        if mf:
            add(mf, 0.0, music_gain, loop=True,
                label="audio.music_file（footage.json 指定的配乐）")
        else:
            mpath = work / "music.wav"
            if not mpath.exists():
                from musicgen import render as music_render
                spec = dict(audio.get("music") or {})
                spec.setdefault("duration", round(total, 3))
                log("  [music] musicgen 现场编排 %.1fs 配乐" % total)
                samples, rep = music_render(**spec)
                filmkit.wav_write(str(mpath), samples)
                log("  [music] %s（%s BPM / %s）"
                    % (mpath.name, rep.get("bpm"), rep.get("key") or rep.get("scale")))
            add(mpath, 0.0, music_gain, loop=True)

    peak = float(np.abs(bus).max()) if bus.size else 0.0
    log("  [mix] 叠加峰值 %.3f" % peak)
    if peak > 0.92:
        bus *= 0.92 / peak
        log("  [mix] 软限幅至 0.92（原 %.3f）" % peak)
    out = work / "mix.wav"
    pcm = (np.clip(bus[:n], -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    log("  [mix] -> %s（%.2fs）" % (out.name, n / float(SR)))
    return str(out)


# ============================================================================
# 5. 字幕：ASS（PlayRes = 输出分辨率，字号/位置都是像素）
# ============================================================================

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},&H00F2F2F2,&H000000FF,&H80101010,&H96000000,-1,0,0,0,100,100,{spacing},0,1,{outline},{shadow},2,{ml},{mr},{mv},1
Style: Note,{font},{note_size},&H00C8C8C8,&H000000FF,&H80101010,&H96000000,0,0,0,0,100,100,1,0,1,1.4,0.8,2,{ml},{mr},{note_mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(sec):
    # 先化成"厘秒"整数再拆 h/m/s：直接 "%05.2f" 会把 59.996 打成 "60.00"，
    # 得到 "0:00:60.00" 这种非法 ASS 时间戳（libass 解析结果不可靠，字幕会错位/丢行）。
    cs = int(round(max(0.0, float(sec)) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)


def write_ass(timeline, footage, path):
    """生成 ASS 字幕（逐句对齐旁白）+ 片尾出处卡。

    三条硬约束（每一条都来自真实返工）：
      1. 换行用单反斜杠 + N（ASS 规范），ASS 头是 WrapStyle:2，不显式断行就会横穿画面；
      2. 每行像素宽必须 <= 安全宽——判据与门禁（F9）用同一把尺子（filmkit.text_w）；
      3. 每条字幕最多两行；ASS 的 Style 只有一个字号，所以只能**全局选一个字号**，
         用该字号把所有句子重新折行，直到全部满足 1/2/3（逐句降字号会让 Style 对不上）。
    """
    video = footage.get("video") or {}
    w = int(video.get("width") or 1920)
    h = int(video.get("height") or 1080)
    sub = footage.get("subtitle") or {}
    size = int(sub.get("size", 46))
    note_size = int(sub.get("note_size", max(24, int(size * 0.62))))
    margin_v = int(sub.get("margin_v", max(28, int(h * 0.035))))
    ml = int(sub.get("margin_l", 110))
    mr = int(sub.get("margin_r", 110))
    max_w = max(240, int(w) - ml - mr)
    safe_w = int(max_w * 0.97)      # 折行留 3% 余量：标点会被整体补到行尾
    family = (sub.get("family") or "sans")
    NL = chr(92) + "N"
    try:
        import filmkit as _K
        _wrap = _K.wrap_cn
        _font = _K.font
        _tw = _K.text_w
    except Exception:                                        # noqa: BLE001
        _K = None

    def _lines_at(text, sz, max_lines=2):
        """按给定字号折行；行数或宽度不满足就返回 None（由调用方降字号）。"""
        if _K is None:
            return [text]
        f = _font(max(14, int(sz)), "reg", family)
        lines = _wrap(text, f, safe_w)
        if len(lines) > max_lines:
            return None
        if any(_tw(x, f) > max_w for x in lines):
            return None
        return lines

    halfwidth = bool(sub.get("halfwidth_commas", True))
    items = []
    for r in timeline["lines"]:
        text = str(r.get("sub") or r["text"])
        if halfwidth:
            text = text.replace(",", "，")
        items.append((r, text))
    credit = footage.get("credit") or {}
    ct = str(credit.get("text") or "")

    gsize = int(size)
    main_lines = None
    while gsize >= 14:
        trial = []
        ok = True
        for _r, text in items:
            ls = _lines_at(text, gsize, 2)
            if ls is None:
                ok = False
                break
            trial.append(ls)
        if ok:
            main_lines = trial
            break
        gsize = int(gsize * 0.94)
    if main_lines is None:
        # 极端兜底：按 14px 折行，再按宽度硬切（宁可三行，也不让字出画）
        gsize = 14
        f14 = _font(14, "reg", family) if _K else None
        main_lines = []
        for _r, text in items:
            base = _wrap(text, f14, safe_w) if _K else [text]
            out2 = []
            for seg in base:
                while _K and seg and _tw(seg, f14) > max_w:
                    lo, hi = 1, len(seg)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if _tw(seg[:mid], f14) <= max_w:
                            lo = mid
                        else:
                            hi = mid - 1
                    out2.append(seg[:max(1, lo)])
                    seg = seg[max(1, lo):]
                if seg:
                    out2.append(seg)
            main_lines.append(out2)

    ev = []
    for (r, _text), ls in zip(items, main_lines):
        ev.append("Dialogue: 0,%s,%s,Main,,0,0,0,,%s"
                  % (_ts(r["start"]), _ts(r["end"] + 0.15), NL.join(ls)))
    if ct:
        nsize = int(note_size)
        clines = None
        while nsize >= 14:
            ls = _lines_at(ct, nsize, 3)
            if ls is not None:
                clines = ls
                note_size = nsize
                break
            nsize = int(nsize * 0.94)
        if clines is None:
            clines = _wrap(ct, _font(14, "reg", family), safe_w) if _K else [ct]
            note_size = 14
        c_start = float(credit.get("at") or max(0.0, timeline["total"] - 2.6))
        c_end = float(credit.get("end") or max(c_start + 1.2, timeline["total"] - 0.4))
        ev.append("Dialogue: 0,%s,%s,Note,,0,0,0,,%s"
                  % (_ts(c_start), _ts(c_end), NL.join(clines)))

    head = ASS_HEAD.format(
        w=w, h=h, font=sub.get("font") or "Microsoft YaHei", size=gsize,
        spacing=sub.get("spacing", 1.2), outline=sub.get("outline", 2.2),
        shadow=sub.get("shadow", 1.4), ml=ml, mr=mr, mv=margin_v,
        note_size=note_size,
        note_mv=max(10, margin_v - int(gsize * 0.7)))
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    log("  [subs] %s（%d 条，主字幕 %dpx）" % (Path(path).name, len(ev), gsize))
    return {"path": str(path), "lines": len(ev)}

def ass_filter(path):
    """ASS 滤镜参数：用相对文件名（配合 cwd）。

    Windows 上写绝对路径会撞盘符冒号：ass=filename='D:/a/b.ass' 直接解析失败
    （报 No option name near '/a/b.ass'）。所以 assemble 会 cd 到 ass 目录再引用文件名。
    """
    return "ass=filename='%s'" % Path(path).name


# ============================================================================
# 6. assemble：拼接 + 字幕 + loudnorm 成片
# ============================================================================

def _concat_escape(path):
    r"""concat 列表里的单引号转义。

    ffmpeg 的 concat demuxer 用 '...' 包路径，**不支持 \' 转义**，标准写法是把
    单引号写成 '\''（先闭合、再转义引号、再重开）。不转义时 Windows 上
    "Bob's work" 这类目录名会让列表在引号处提前结束，ffmpeg 报 No such file。
    """
    return str(path).replace("'", "'\\''")


def concat_video(cut_rows, work):
    """按顺序拼接镜头（流拷贝，不二次编码）。"""
    work = Path(work)
    if not cut_rows:
        raise SystemExit("没有可拼接的镜头（cut.json 为空）：先跑 --stage cut")
    lst = work / "concat.txt"
    lines = ["file '%s'" % _concat_escape(Path(c["video"]).resolve().as_posix())
             for c in cut_rows]
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = work / "silent.mp4"
    p = _run([filmkit.FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(lst), "-c", "copy", str(out)])
    # _run 不检查退出码：拼接失败时原来只会在日志里写 "0.00s" 并把不存在的文件
    # 交给 assemble。这里必须自己判。
    if p.returncode != 0 or not Path(out).exists():
        raise SystemExit("concat 拼接失败（ffmpeg exit=%d）：\n%s"
                         % (p.returncode,
                            (p.stderr or b"").decode("utf-8", "replace").strip()[-800:]))
    try:
        dur = filmkit.media_dur_checked(str(out), "拼接后的无声视频")
    except RuntimeError as e:
        raise SystemExit("concat 产出的 silent.mp4 读不出时长：%s" % e) from None
    log("  [concat] %d 镜 -> silent.mp4（%.2fs）" % (len(cut_rows), dur))
    return str(out)


def assemble(footage, work, out_path, *, ass_path=None, crf=19, preset="medium"):
    """拼视频 + 混音 -> 成片（两遍 loudnorm 到 -16 LUFS / 真峰 ≤ -1.5 dBTP）。

    这一步自己再编一遍视频（而不是 copy）：因为要烧字幕。
    """
    work = Path(work).resolve()
    # 统一成绝对路径：后面会把 cwd 切到 ass 所在目录（Windows 盘符冒号的替代方案），
    # 相对路径一旦遇上 cwd 就找不到文件（实测 exit=4294967294 / No such file）。
    silent = (work / "silent.mp4").resolve()
    mix = (work / "mix.wav").resolve()
    if not silent.exists():
        raise SystemExit("缺 %s（先跑 cut/concat）" % silent)
    if not mix.exists():
        raise SystemExit("缺 %s（先跑 mix）" % mix)
    # 用 checked 版：media_dur 读不到返回 0.0，而 0.0 为假值会让下面这条等长校验整条跳过。
    vd = filmkit.media_dur_checked(str(silent), "无声视频")
    ad = filmkit.media_dur_checked(str(mix), "混音音轨")
    if abs(vd - ad) > 0.05:
        raise SystemExit("音视频时长不一致：video %.3fs vs audio %.3fs（差 %.3fs）"
                         % (vd, ad, abs(vd - ad)))
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    af = filmkit.loudnorm_two_pass(str(mix), target_i=-16.0, target_tp=-2.2) or ""
    cmd = [filmkit.FFMPEG, "-v", "error", "-y", "-i", str(silent), "-i", str(mix)]
    if ass_path:
        ass_path = Path(ass_path).resolve()
        cmd += ["-vf", ass_filter(ass_path)]
    video = footage.get("video") or {}
    cmd += ["-c:v", "libx264", "-crf", str(footage.get("crf", crf)),
            "-preset", str(footage.get("preset", preset)),
            "-pix_fmt", "yuv420p", "-r", str(video.get("fps") or 30),
            "-c:a", "aac", "-b:a", str(footage.get("audio_bitrate", "192k")),
            "-ar", str(SR), "-ac", "2"]
    if af:
        cmd += ["-af", af]
    # 显式 -t 而不是 -shortest：-shortest 会按较短一路截断（素材线实测吃掉过 16 秒片尾），
    # 这里音视频已校验等长，直接用视频时长封口。
    if vd:
        cmd += ["-t", "%.3f" % vd]
    cmd.append(str(out_path))
    p = _run(cmd, cwd=str(Path(ass_path).parent) if ass_path else None)
    # _run 只是 subprocess.run，**不检查 returncode**：ffmpeg 失败时它照样返回，
    # 于是"输出没生成"会被当成成功（实测 out/ 是空的但日志写 [assemble] ->）。
    # 素材线每个 stage 都必须自己判退出码，否则门禁会对着一个不存在的文件跑。
    if p.returncode != 0:
        raise SystemExit("assemble 编码失败（exit=%d）：\n%s"
                         % (p.returncode, (p.stderr or b"").decode("utf-8", "replace")[-1500:]))
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise SystemExit("assemble 命令返回成功但没产出文件：%s" % out_path)
    log("  [assemble] -> %s（%.1f MB）" % (out_path, out_path.stat().st_size / 1048576.0))
    return str(out_path)
