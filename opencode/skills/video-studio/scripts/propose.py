# -*- coding: utf-8 -*-
"""propose.py — 提案器：先出 N 个方案，用户确认后才允许出片。

为什么单独一个入口而不是塞进 render.py
--------------------------------------
render.py 的 13 个 stage 是一条"内容 → 成片"的生产线；提案是**生产之前**的决策环节，
它的产物（proposal.json）要能被 frames 阶段当成前置门禁来查。混进 STAGES 会把
"这条 stage 的输入输出是什么"搅乱（stagewatch 的依赖表是按生产链写的）。
所以：独立入口 + 独立产物 + 由 render.py 在 frames 前**只读校验**。

流程
----
    propose  ->  work/proposal.json (rev1, status=draft, N 个候选 + 静帧 + 成本)
    confirm  ->  status=confirmed, chosen="B"，并落 work/design.json（冻结）
    revise   ->  rev2（不覆盖 rev1，带 feedback 原文）
    diff     ->  两个 rev 的设计向量差异

每次都不一样：同 brief 不传 seed 会得到**不同**的候选（满足"每条视频都不同"）；
可复现：显式 --seed 时同 seed 得到同一组候选（designkit 内部 sha1 派生，禁 random）。
离线：预览只走合成时间轴（previewkit），**不联网、不合成旁白**；--with-audio 才多生成
      12 秒样音（仍全程离线，musicgen/scorekit 任选其一）。

确认要绑两道指纹
----------------
confirm 除了 designHash（设计指纹），还写 scriptHash（剧本规范化 JSON 的 sha1 前 16 位）。
只绑设计不够：设计没变而剧本大改，字幕/断句/时长全变，那次确认等于作废。
proposal_state 因此多一档 stale——剧本对不上就要求重新提案；剧本读不出来（文件搬走、
不是合法 JSON）时**不判**，因为"算不了"不该变成一条假警报。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

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

SCHEMA = 1
PROPOSAL_FILE = "proposal.json"
PROPOSALS_DIR = "proposals"
CANDIDATE_IDS = "ABCDEFGH"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise SystemExit("[proposal] %s 不是合法 JSON：%s" % (path, e)) from e


_TMP_SEQ = [0]


def _atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    # 临时名必须唯一：固定 ".part" 时两个进程同时写同一文件会互相截断，
    # os.replace 还可能把对方写了一半的临时文件换上去（并发 --confirm 就撞这里）。
    _TMP_SEQ[0] += 1
    tmp = "%s.part-%d-%d" % (path, os.getpid(), _TMP_SEQ[0])
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(obj, n=16):
    return hashlib.sha1(_canonical(obj).encode("utf-8")).hexdigest()[:n]



# ============================================================================
# 片间去重：设计台账
# ============================================================================
#
# 用户原则 5：**不同视频的整体风格不能大面积重复**。只靠采样 nonce 的"每条视频都不同"
# 靠的是采样 nonce —— 同一 brief 两次采样确实不同，但**没有记住已经交付过什么**：
# 连续拍同题材的三条片，仍有概率撞到相近的配色/版式组合（设计空间大，但不是保证）。
# 台账把"最近交付过的设计"记在用户级目录里，提案时主动避开它们：
#   · 只看**离散维度**（配色/母题/转场/缓动/编号/配乐/声线/情绪/版式/调式），
#     与 propose 展示给用户的"设计距离"同一口径；
#   · 默认开启，可用 --no-ledger 或环境变量 VIDEO_STUDIO_LEDGER=off 关闭；
#   · 路径可用环境变量覆盖（写文档/做测试时指到临时目录，不要去动用户真实台账）。
# 为什么默认开启：用户不会主动想起"帮我避开上一部片"；默认关闭等于这个功能不存在。
_LEDGER_ENV = "VIDEO_STUDIO_LEDGER"


def ledger_path():
    """台账文件路径：环境变量优先，否则 ~/.config/opencode/video-studio/design-ledger.json。"""
    raw = str(os.environ.get(_LEDGER_ENV) or "").strip()
    if raw.lower() in ("off", "0", "false", "no"):
        return ""
    if raw:
        return os.path.abspath(os.path.expanduser(raw))
    home = os.path.expanduser("~")
    return os.path.join(home, ".config", "opencode", "video-studio", "design-ledger.json")


def _ledger_rows(path):
    """读台账 -> (rows, corrupt)。corrupt 区分"文件不在"与"文件坏了"。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], False
    except (OSError, json.JSONDecodeError):
        return [], True
    rows = data.get("designs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], True
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("design"), dict)], False


def _ledger_backup_corrupt(path):
    """坏台账先留档再重建：它记着"最近交付过哪些设计"，静默清空 = 去重能力失忆。"""
    dst = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.replace(path, dst)
        return dst
    except OSError:
        return ""


def _ledger_lock(path, timeout=8.0):
    """跨进程写锁（O_EXCL 建 <ledger>.lock）。拿不到返回空串：台账不该阻塞出片。"""
    lock = path + ".lock"
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except FileExistsError:
            # 进程崩溃会留下残留锁：超过 30 秒没更新就当过期，别让台账永久卡死。
            try:
                if time.time() - os.path.getmtime(lock) > 30.0:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return ""
            time.sleep(0.02)
        except OSError:
            return ""


def _ledger_unlock(lock):
    try:
        os.unlink(lock)
    except OSError:
        pass


def ledger_load(limit=60):
    """读最近 limit 条设计记录（读不到返回空表：台账不该阻塞出片；但坏文件要出声）。"""
    path = ledger_path()
    if not path:
        return []
    rows, corrupt = _ledger_rows(path)
    if corrupt:
        print("[proposal] 台账读不出（%s）：本次按空台账处理，下次 --confirm 会先备份再重建"
              % path, file=sys.stderr)
        return []
    return rows[-int(limit):]


def ledger_record(design, work=None, label=""):
    """把一份已确认的设计记进台账（去重：同一指纹只保留最新一条）。"""
    path = ledger_path()
    if not path:
        return False
    try:
        import designkit as _DK
        fp = _DK.design_fingerprint(design)
    except Exception:                                       # noqa: BLE001
        return False
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError:
        return False
    lock = _ledger_lock(path)
    if not lock:
        print("[proposal] 台账写锁等不到（%s）：本次跳过记录" % path, file=sys.stderr)
        return False
    try:
        rows, corrupt = _ledger_rows(path)
        if corrupt:
            backup = _ledger_backup_corrupt(path)
            print("[proposal] 台账文件损坏，已备份为 %s 并重建" % (backup or "（备份失败）"),
                  file=sys.stderr)
        rows = [r for r in rows if r.get("fingerprint") != fp]
        rows.append({"fingerprint": fp, "label": str(label or design.get("label") or ""),
                     "at": _now(), "work": os.path.abspath(str(work)) if work else "",
                     "design": design})
        rows = rows[-500:]
        try:
            _atomic_write(path, json.dumps({"schema": 1, "designs": rows},
                                           ensure_ascii=False))
            return True
        except OSError as e:
            print("[proposal] 台账写不进去（%s）：%s" % (path, e), file=sys.stderr)
            return False
    finally:
        _ledger_unlock(lock)


def ledger_min_distance(designs, recent):
    """这批候选与"最近交付过的设计"之间的最小设计距离（离散维度数）。"""
    if not recent:
        return None
    import designkit as _DK
    worst = 99
    for d in designs or []:
        for r in recent:
            try:
                n = int(_DK.design_distance(d, r["design"]).get("n", 0))
            except Exception:                               # noqa: BLE001
                continue
            worst = min(worst, n)
    return None if worst == 99 else worst


# 与上一部片至少要差这么多维，才算"看得出不是同一套"（与候选两两约束同口径）。
LEDGER_MIN_GAP = 2
# 撞车时最多重采几轮（每轮都是一次全新 nonce 采样，成本 = 一次采样，毫秒级）。
LEDGER_RETRIES = 4
def proposal_path(work):
    return os.path.join(str(work), PROPOSAL_FILE)


def rev_dir(work, rev):
    return os.path.join(str(work), PROPOSALS_DIR, "rev%d" % int(rev))


def _next_rev(work, prev):
    """下一版 rev = max(proposal.json 的 rev, 已有 revN 快照) + 1。

    为什么不能只看 proposal.json：它被写坏/被移走后（坏 JSON 只能移走才能继续）
    重跑提案会算出 rev=1，而 work/proposals/rev1 里已有上一版的快照——
    os.makedirs(exist_ok=True) 会静默把旧快照连同候选图覆盖掉，历史 rev 就没了。
    """
    best = int((prev or {}).get("rev") or 0)
    base = os.path.join(str(work), PROPOSALS_DIR)
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for name in names:
        if not (name.startswith("rev") and name[3:].isdigit()):
            continue
        if os.path.isfile(os.path.join(base, name, PROPOSAL_FILE)):
            best = max(best, int(name[3:]))
    return best + 1


def _designkit():
    try:
        import designkit
    except ImportError as e:
        raise SystemExit(
            "[proposal] 缺少 designkit.py（%s）——它是设计 DSL 与采样器，必须与 propose.py 同目录"
            % e) from e
    return designkit


def _previewkit():
    try:
        import previewkit
    except ImportError as e:
        raise SystemExit(
            "[proposal] 缺少 previewkit.py（%s）——离线预览要它，必须与 propose.py 同目录"
            % e) from e
    return previewkit


def _script_hash_of(path):
    """剧本指纹：规范化 JSON 的 sha1 前 16 位；读不出（没有文件/不是 JSON/没有 lines）返回 None。

    "算不了就不判"是刻意的：剧本被搬走或临时写坏时，把 confirmed 判成 stale 会逼用户
    重新确认一件其实没变的事，久了就没人信这条门禁了。
    """
    if not path or not os.path.isfile(str(path)):
        return None
    try:
        script = _load_json(path, default=None)
    except SystemExit:
        return None
    if not isinstance(script, dict) or not isinstance(script.get("lines"), list):
        return None
    return _sha(script)


def _load_script(path):
    script = _load_json(path)
    if not isinstance(script, dict):
        raise SystemExit("[proposal] 剧本 %s 不是 JSON 对象" % path)
    if not script.get("lines"):
        raise SystemExit("[proposal] 剧本 %s 里没有 lines[]（提案至少需要一段内容）" % path)
    return script


# ---------------------------------------------------------------- 提案生成
_NEGATIVE_WORDS = ("不要", "不想要", "别用", "别", "避开", "排除", "避免", "去掉", "不用")



# ---------------------------------------------------------------- 时长解析
# brief 里最常见的硬要求之一就是时长（"做个 2 分钟的""60 秒短视频"）。不解析它
# 就**完全不生效**：_brief_of 只填 topic/must_have/avoid，duration_target 恒为 None，
# 连 design.structure.target_sec 都不会有值。用户以为要求被接住了，成片却按台词长度来。
# 这里把它解析出来：① 影响候选的段落数（designkit 按目标时长决定 segments）；
# ② 渲染时对账"目标 vs 实测"，差超过 15% 当场告警——**不截断旁白**，只把事实说清。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text):
    """把"九十 / 一百二十 / 半"这类简单中文数字转成 float；认不出返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    if s == "半":
        return 0.5
    try:
        return float(s)
    except ValueError:
        pass
    total, section = 0.0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
        elif ch == "百":
            section = (section or 1) * 100
        else:
            return None
    return float(total + section)


def parse_duration(text):
    """从一句话里解析时长（秒）。认不出返回 None。

    支持（中文单位）：「90秒」「2分钟」「2.5分钟」「一分半」「3分30秒」「1分30秒」「两分钟」。
    支持（ASCII 单位，README/SKILL/intake 的示例写的就是这种）：「45s」「20 s」「90sec」
    「1.5min」「2m」「3minutes」——大小写不敏感，数字与单位之间允许空格。
    为什么只认这些：宁可比对少认，也不要猜错——猜错会让成片段落数按错误时长排，
    用户更难发现。认不出的句子不会被静默吞掉：整句仍作为 topic 参与题材理解。
    为什么只取**首个**匹配、不把多个时长相加：「每 5 分钟走 20 秒」是在说节拍，
    相加成 320 秒会把整片长度排错；取首匹配（5 分钟 = 300 秒）至少和句子的主语一致。
    """
    import re as _re
    s = str(text or "")
    best = None
    m = _re.search(
        r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*分(?:钟)?\s*"
        r"(半|([0-9]+|[零一二两三四五六七八九十百]+)\s*秒)?", s)
    if m:
        mins = _cn_number(m.group(1))
        if mins is not None:
            secs = 0.0
            if m.group(2) == "半":
                secs = 30.0
            elif m.group(3):
                v = _cn_number(m.group(3))
                secs = float(v) if v is not None else 0.0
            best = mins * 60.0 + secs
    if best is None:
        m = _re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*秒", s)
        if m:
            v = _cn_number(m.group(1))
            if v is not None:
                best = float(v)
    if best is None:
        # ASCII 单位：文档把 `--set duration:45s` 写成示例（README / SKILL / intake 三处），
        # 而上面两条分支只认「分/秒」——照文档执行会被 intake 判成"不是合法选项"。
        # 单位按长到短排（min before m, sec before s），否则 "2min" 会被截成 "2m" 后
        # 在 "in" 处撞上右边界而整体不认。
        # 左边界：数字前不能是字母/数字（"ipv6s"、"h264s" 里的字母不是单位）；
        # 连 "." 也一起排除，"v1.45s" 这种版本号写法不能被读成 45 秒。
        # 右边界：单位后不能紧跟字母（"2months"/"45secondsx" 必须整体不认，
        # 而不是截成 2m/45s）——宁可少认，也不能猜错。
        m = _re.search(
            r"(?<![0-9A-Za-z.])([0-9]+(?:\.[0-9]+)?)\s*"
            r"(min(?:ute)?s?|m|sec(?:ond)?s?|s)(?![A-Za-z])",
            s, _re.IGNORECASE)
        if m:
            v = float(m.group(1))
            best = v * 60.0 if m.group(2).lower().startswith("m") else v
    if best is None or best <= 0:
        return None
    if best < 3.0 or best > 1800.0:
        return None            # 与 designkit 的契约范围一致（3–1800 秒）
    return round(best, 2)
# 「不少于 N 分钟 / 至少 N 分钟 / N 分钟以上」= 下限语义。
# 为什么必须区分：目标语义下「还差 30%」只是告警；下限语义下「还差 30%」= 没达标。
# 成片长度永远由 TTS 实测决定，两种语义都不能截断旁白，区别只在**怎么报**：
# exact -> 差值双向告警；at_least -> 只有不足才告警，超出是好事（不提示）。
_FLOOR_WORDS = ("不少于", "不少于", "至少", "最少", "不低于", "不低于",
                "起", ">=", "≥", "以上")


def parse_duration_spec(text):
    """解析时长要求 -> (秒数, 模式) 或 (None, None)。

    模式："at_least"（下限，例如「不少于 5 分钟」） / "exact"（精确目标，例如「2 分钟」）。
    只认 parse_duration 认得的写法（宁可比对少认，也不要猜错），所以 ASCII 写法同样走这条路径：
    「不少于45s」-> (45.0, "at_least")，「45s 以上」也判下限。
    认不出就返回 (None, None)，整句仍作为 topic 参与题材理解，不会被静默吞掉。
    """
    sec = parse_duration(text)
    if sec is None:
        return None, None
    raw = str(text or "")
    # 只在与时长数字**同一短句**里找下限词：避免「结尾用中文，不少于三条建议」这类
    # 别的句子的「不少于」被算进时长语义（实测这种误判会让整片被当成下限模式）。
    import re as _re
    for seg in _re.split(r"[，。；、,.;!?！？\n]+", raw):
        if parse_duration(seg) is None:
            continue
        if any(w in seg for w in _FLOOR_WORDS):
            return sec, "at_least"
        return sec, "exact"
    return sec, "exact"

# ---------------------------------------------------------------- 出片前问诊
# 一次问完的决策表由 intake.py 落盘（work/intake.json）。propose 读它的原因是：
# 「先出方案」只解决"外观谁拍板"，素材/时长/声调这些输入仍然要靠问。实际会话里
# 没人把关时，agent 会直接从半句话里猜出全部输入 —— 用户到成片才第一次看到自己的
# 片子长什么样，返工全在渲染之后。所以**首次提案前必须先答完问诊**：
# 不答就不许出方案（--no-intake 是明确的逃生口，且会记进提案里留痕）。
INTAKE_FILE = "intake.json"


def intake_path(work):
    return os.path.join(str(work), INTAKE_FILE)


def _intake_state(work):
    """读问诊结果：返回 {"present", "status", "missing", "brief", "path", "mode"}。

    mode 是问诊「制作方式」题的落地结果（chosen / resolved / route）；素材剪辑线（footage）
    下提案层不适用，上层据此提示，不再假装"这个方案能直接套到成片"。

    读不出/不是 JSON 一律当"没有问诊"处理——问诊文件坏掉不该让上层看到一个
    可疑的"已答完"，那会跳过提问直接出方案（正是这一刻要避免的事）。
    """
    p = intake_path(work)
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"present": False, "status": "absent", "missing": [], "brief": "",
                "path": p, "mode": {}}
    if not isinstance(data, dict):
        return {"present": False, "status": "broken", "missing": [], "brief": "",
                "path": p, "mode": {}}
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    missing = [str(x) for x in (data.get("missing") or [])]
    status = str(data.get("status") or ("complete" if not missing else "draft"))
    mode = data.get("mode") if isinstance(data.get("mode"), dict) else {}
    return {"present": True, "status": status, "missing": missing,
            "brief": str(data.get("brief") or ""), "path": p,
            "schema": int(data.get("schema") or 0),
            "answers": dict(answers), "mode": dict(mode)}


def _intake_gate(work, brief_text, no_intake):
    """首次提案前的问诊闸：答完了才放行出方案。返回 (brief, state, bypassed)。"""
    state = _intake_state(work)
    if no_intake:
        state = dict(state, status="bypassed")
        if not str(brief_text or "").strip() and state.get("present"):
            brief_text = state.get("brief") or ""
        return brief_text, state, True
    if state.get("status") == "complete":
        # 用户答过问诊：以答案合成的 brief 为准（命令行 --brief 只当作额外补充）。
        merged = [str(brief_text or "").strip(), str(state.get("brief") or "").strip()]
        return "，".join([x for x in merged if x]), state, False
    miss = " / ".join(state.get("missing") or []) or "题材"
    raise SystemExit(
        "[proposal] 还没完成出片前问诊（%s）。\n"
        "  这是刻意的：题材/制作方式/时长/气质/声线/配乐这些只有用户能定，先问完再出方案，"
        "返工才不会落在渲染之后。\n"
        "  怎么做：\n"
        "    1) python intake.py --work %s --questions --json   # 拿去问用户（一次问完）\n"
        "    2) python intake.py --work %s --set topic:\"...\" --set mode:auto --set tone:bright-lively ...\n"
        "    3) python intake.py --work %s --finalize          # 合成 brief\n"
        "  确实不需要问（批量补渲 / 已在别处答过）：加 --no-intake，会记进提案留痕。\n"
        "  当前状态：%s；还没答：%s" % (state.get("status"), work, work, work,
                                        state.get("status"), miss))


def _brief_of(text, script):
    """把用户一句话变成 brief dict。剧本的标题是隐含 topic（避免空 brief）。

    以前这里把整句话塞进 topic 就完事，**must_have/avoid 恒为空数组**。
    后果是 `--brief "深蓝配色的科技解说"` 里的"深蓝"完全不生效 —— 实测采出来的是
    h087 / h316（橙 / 紫），用户看到的需求与结果无关。designkit 明明有一张
    32 个词的关键词表（_constraint_words），但没人喂给它。

    现在按句子切开逐个识别：
      · 含否定词的片段 -> avoid（"不要放射线" -> avoid 放射）；
      · 其余片段 -> must_have（"深蓝配色" -> must_have 深蓝）；
      · 一句都没认出来的部分原样留着——顶层会告诉用户"这条没生效"，绝不静默丢弃。
    """
    text = str(text or "").strip()
    topic = text or str(script.get("title") or "").strip()
    if not topic:
        raise SystemExit("[proposal] 需要 --brief 或剧本里至少有 title（否则不知道在给什么题材做方案）")
    try:
        import designkit as DK
        probe = DK._constraints_in
    except Exception:                                       # noqa: BLE001
        probe = None
    must, avoid = [], []
    if probe is not None and text:
        # 按标点/连接词切成短句，逐句判定极性
        import re as _re
        for seg in _re.split(r"[，。；、,.;!?！？]+|\s+", text):
            seg = seg.strip()
            if not seg:
                continue
            hits = probe(seg)
            if not hits:
                continue                       # 认不出来的短句留给 topic，由上层提示
            neg = any(w in seg for w in _NEGATIVE_WORDS)
            (avoid if neg else must).append(seg)
    # 时长要求也要接住（漏了就是恒为 None，用户写了「2 分钟」等于没写）。
    # 下限与精确目标分开记：见 parse_duration_spec 的说明。
    dt, dt_mode = parse_duration_spec(text)
    return {"topic": topic, "duration_target": dt, "duration_mode": dt_mode,
            "must_have": must, "avoid": avoid}


def _candidate_row(design, script, outdir, n_frames, previewkit, designkit,
                   with_audio=False):
    """跑一个候选的离线预览，返回 proposal.json 里的候选行。"""
    t0 = time.time()
    pv = previewkit.preview_candidate(design, script, outdir, n_frames=n_frames,
                                      with_audio=with_audio)
    secs = round(time.time() - t0, 2)
    pv["secs"] = secs
    preview = {"sheet": pv.get("sheet"), "frames": pv.get("frames"), "secs": secs}
    # 预览会按合成时长排轴，段太短时 grammar 会降级构图（chart -> list 等）；
    # 把降级信息带进提案，人和机器都能看出"哪一段预览里画的和成片不一样"。
    if pv.get("downgrades"):
        preview["downgrades"] = pv["downgrades"]
    if with_audio:
        # preview.audio 只放路径（契约字段）；引擎/耗时/失败原因分开写，
        # 是为了让人一眼看出"这份样音是谁出的、等了多久"，而不用去翻日志。
        preview["audio"] = pv.get("audio") or ""
        preview["audio_engine"] = pv.get("audio_engine") or ""
        preview["audio_secs"] = pv.get("audio_secs")      # 生成耗时
        preview["audio_dur"] = 12.0 if pv.get("audio") else None   # 音频长度（秒）
        if pv.get("audio_error"):
            preview["audio_error"] = pv["audio_error"]
    return {
        "id": pv.get("id"),
        "label": design.get("label") or design.get("id") or "",
        "pitch": design.get("pitch") or "",
        "design": design,
        "design_fp": designkit.design_fingerprint(design),
        "preview": preview,
        "cost": {"preview_s": secs},
        "diversity": None,   # 生成完之后统一算（需要两两比较）
    }


def _print_critic(rows):
    """用 critic.py 给候选联系表打分并打印（失败只提示，不影响提案）。

    为什么不 import critic 而是走子进程：critic 需要 numpy/PIL，而提案器本身
    只需要 stdlib + designkit；把可选依赖塞进主流程会让"没装 numpy 也能提案"失效。
    """
    import subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))
    found = [(r["id"], r["preview"]["sheet"]) for r in rows if r.get("preview", {}).get("sheet")]
    if not found:
        print("   审美打分: 跳过（没有联系表）")
        return
    args = [sys.executable, os.path.join(here, "critic.py")]
    for _cid, sheet in found:
        args += ["--image", sheet]
    try:
        r = _sp.run(args, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=300)
    except Exception as e:                                    # noqa: BLE001
        print("   审美打分: 失败（%s）" % e)
        return
    if r.returncode != 0:
        print("   审美打分: 失败（critic.py 退出 %d：%s）"
              % (r.returncode, (r.stderr or r.stdout)[-200:]))
        return
    lines = [ln.strip() for ln in (r.stdout or "").splitlines()
             if ln.strip() and not ln.startswith("-") and not ln.startswith("审美质检")
             and not ln.startswith("提示：")]
    print()
    print("   审美打分（离线启发式，只用于排序，分差 >15 通常说明该换一个）:")
    for ln in lines[:len(found)]:
        print("     " + ln)


def _fill_diversity(rows, designkit):
    """两两算设计距离，把"跟谁像/不像"写进每个候选（伪多方案门禁的输入）。"""
    for i, a in enumerate(rows):
        vs = []
        for j, b in enumerate(rows):
            if i == j:
                continue
            d = designkit.design_distance(a["design"], b["design"])
            vs.append({"id": b["id"], "n": d.get("n", 0), "axes": d.get("axes") or []})
        rows[i]["diversity"] = {"vs": vs,
                                "min_n": min([v["n"] for v in vs]) if vs else 0}


def make_proposal(brief_text, script, work, n=3, seed=None, n_frames=4, json_out=False,
                  with_audio=False, no_ledger=False, no_intake=False, critic=False):
    """生成一版提案（rev = 已有最大 rev + 1），落盘并打印人读摘要。"""
    designkit = _designkit()
    previewkit = _previewkit()
    prev = _load_json(proposal_path(work), default=None) or {}
    # 问诊闸只管**首次**提案：后续 rev 是在已答完的前提下改方案，再拦一次只是噪音。
    if not prev:
        brief_text, intake_state, intake_bypassed = _intake_gate(work, brief_text, no_intake)
        if not json_out and intake_state.get("status") == "complete":
            print("   问诊: 已答完（%s）" % intake_state.get("path"))
        if not json_out and intake_bypassed:
            print("   问诊: 已用 --no-intake 跳过（本次提案里会留痕）")
        # 旧问诊单（schema 1）没有「制作方式」：不拦（向后兼容），但要提示补问——
        # 否则这片子的产线是"没有记录"的，事后查不出当初选的是哪条线。
        if (not json_out and intake_state.get("present")
                and 0 < int(intake_state.get("schema") or 0) < 2
                and not (intake_state.get("mode") or {}).get("resolved")):
            print("   问诊: 问诊单是旧版（schema 1），缺「制作方式」（mode）——建议补问一次；"
                  "本次提案照旧出方案，但 proposal.intake.mode 会是空的。")
        # 制作方式=素材剪辑时，提案层本身不适用：素材线的外观由素材调色/遮幅决定。
        # 不失败（hybrid 的图文段仍要设计），但必须说清楚，别让人以为提案能直接套到成片。
        if not json_out and (intake_state.get("mode") or {}).get("resolved") == "footage":
            print("   问诊: 制作方式=网上素材剪辑（footage 线）——提案层不适用；"
                  "出片请用 footage.py（footage.json 契约见 references/footage.md），"
                  "本提案仅供 hybrid 的图文段参考。")
        # hybrid 的提示单独一条：这时候提案**有用**，但只对图文段有效，
        # 而且必须接着跑 matchstyle 才能和素材看齐（这是"两段像一部片"的唯一正路）。
        if not json_out and (intake_state.get("mode") or {}).get("resolved") == "hybrid":
            print("   问诊: 制作方式=两者结合（hybrid）——默认形态是「素材打底 + 小面积叠加」，"
                  "它**不需要**这份提案：画面是素材，自绘只有小面积信息层。\n"
                  "         只有显式走 --hybrid-form 1（按段拼接）时，本提案才决定图文段外观。")
    else:
        intake_state, intake_bypassed = _intake_state(work), False
    brief = _brief_of(brief_text, script)

    prev = _load_json(proposal_path(work), default=None) or {}
    rev = _next_rev(work, prev)
    parent = prev.get("rev") if prev else None
    feedback = str(brief_text or "").strip() if rev > 1 else ""

    # 唯一的出方案方式是**按 brief 现场采样**：没有预设、没有模板可套。
    #
    # 片间去重：读台账里最近交付过的设计，若这批候选与其中某一份"太像"
    # （离散维度差距 < LEDGER_MIN_GAP），换 nonce 重采一次，最多试 LEDGER_RETRIES 轮，
    # 取"最后一批"里与历史距离最大的那组。重试仍近就**明说**（不静默放过）。
    recent = [] if no_ledger else ledger_load()
    if no_ledger and not json_out:
        print("   片间去重: 已用 --no-ledger 关闭（本次不与历史设计比对）")
    designs = designkit.sample_designs(brief, n=n, seed=seed)
    ledger_gap = ledger_min_distance(designs, recent)
    if recent and ledger_gap is not None and ledger_gap < LEDGER_MIN_GAP:
        for _try in range(max(1, int(LEDGER_RETRIES))):
            cand = designkit.sample_designs(brief, n=n, seed=seed)
            gap = ledger_min_distance(cand, recent)
            if gap is not None and gap > (ledger_gap or 0):
                designs, ledger_gap = cand, gap
            if ledger_gap is not None and ledger_gap >= LEDGER_MIN_GAP:
                break
    # 认不出来的要求：采样器不会替你"悄悄放宽"，但也不会因此失败，而是挂在候选上。
    # 这里汇总一次，人读摘要里必须说出来——用户提的要求没生效，他得知道。
    unmatched = []
    for d in designs:
        for word in (d.get("_unmatched_constraints") or []):
            if word not in unmatched:
                unmatched.append(word)
    if len(designs) < 2:
        raise SystemExit("[proposal] 采样器只给了 %d 个候选（至少要 2 个才能算'多方案'）"
                         % len(designs))

    if not json_out and brief.get("duration_target"):
        print("   目标时长: %.1fs（按 brief 解析；成片时长仍由实测旁白决定，渲染阶段会对账）"
              % float(brief["duration_target"]))
    outdir = rev_dir(work, rev)
    os.makedirs(outdir, exist_ok=True)

    rows, t_all0 = [], time.time()
    for i, design in enumerate(designs):
        cid = CANDIDATE_IDS[i] if i < len(CANDIDATE_IDS) else "C%d" % (i + 1)
        design = dict(design)
        design.setdefault("id", "design-%s" % cid.lower())
        design["id"] = "%s-%s" % (cid.lower(), design["id"].split("-")[-1]) if "-" in design["id"] else "%s-%s" % (cid.lower(), design["id"])
        cdir = os.path.join(outdir, cid)
        os.makedirs(cdir, exist_ok=True)
        row = _candidate_row(design, script, cdir, n_frames, previewkit, designkit,
                             with_audio=with_audio)
        row["id"] = cid
        rows.append(row)
        if not json_out:
            print("   候选 %s  %-22s  %d 帧  %.2fs  -> %s"
                  % (cid, row["label"][:22], len(row["preview"]["frames"] or []),
                     row["preview"]["secs"], row["preview"]["sheet"]))

    _fill_diversity(rows, designkit)
    # 台账距离：< LEDGER_MIN_GAP 说明"这批候选和最近交付的某部片太像"。
    # 不阻断出片（用户可能就是想沿用同一套），但必须在摘要里说出来——
    # 这正是原则 5 要的"不要大面积重复"，而用户自己不会去比对上一部片。
    if not json_out and recent:
        print("   片间去重: 已对照最近 %d 部片（最接近的一对差 %d 维，要求 >=%d；台账 %s）"
              % (len(recent), ledger_gap if ledger_gap is not None else -1,
                 LEDGER_MIN_GAP, ledger_path() or "关闭"))
    if not json_out and ledger_gap is not None and ledger_gap < LEDGER_MIN_GAP:
        # --json 的契约是"stdout 只输出 JSON"：机读分支里这条告警会让 json.loads 直接失败。
        # 同样的信息已经在 payload["ledger"]（min_gap / want_gap）里，机读不需要人头提示。
        print("   ! 这批候选与最近交付过的设计最接近的一对只差 %d 个维度（建议 ≥%d）："
              % (ledger_gap, LEDGER_MIN_GAP))
        print("     已有 %d 部片的台账：%s" % (len(recent), ledger_path()))
        print("     想强制避开：--revise \"换个完全不同的方向\"；想忽略：--no-ledger")
    wall = round(time.time() - t_all0, 2)
    # 并排对比图必须**无条件**产出：之前它写在 `if not json_out` 分支里，于是
    # `--json` 调用（机读、被 CI 或上层脚本用）永远拿不到 _compare.png，
    # 而"人眼并排看 N 个方案"正是这一步最重要的产物。
    compare = _write_compare_sheet(rows, outdir)

    payload = {
        "schema": SCHEMA,
        "rev": rev,
        "parentRev": parent,
        "at": _now(),
        "status": "draft",
        "brief": brief,
        "feedback": feedback,
        "chosen": None,
        "seed": seed,
        "wall_s": wall,
        # 问诊留痕：这版方案是不是在"问完用户"之后出的。问诊闸放行时 status=complete；
        # 明确 --no-intake 时为 bypassed —— 事后能查清"这片子当初问过没有"。
        "intake": {"path": intake_state.get("path"),
                   "status": intake_state.get("status"),
                   "bypassed": bool(intake_bypassed),
                   "answers": intake_state.get("answers") or {},
                   # 制作方式（问诊第 2 题）的落地结果：chosen / resolved / route。
                   # 素材线（footage）下提案层不适用，这里留痕，事后可查"为什么还出了方案"。
                   "mode": intake_state.get("mode") or {}},
        # 片间去重证据：这批候选与"最近交付过的设计"的最小维度距离。
        # None = 没有台账（第一次出片 / 已用 --no-ledger 关闭）。
        "ledger": {"path": ledger_path() or None, "recent": len(recent),
                   "min_gap": ledger_gap, "want_gap": LEDGER_MIN_GAP},
        "candidates": rows,
    }
    path = proposal_path(work)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1))
    # 每版留一份快照（rev 不覆盖）
    _atomic_write(os.path.join(outdir, "proposal.json"),
                  json.dumps(payload, ensure_ascii=False, indent=1))

    if not json_out:
        print()
        print("== 提案 rev%d：%d 个候选，总耗时 %.2fs" % (rev, len(rows), wall))
        print("   %-3s %-22s %-9s %-8s %s" % ("id", "立意", "静帧", "距离", "状态"))
        for r in rows:
            print("   %-3s %-22s %8.2fs %-8d %s"
                  % (r["id"], (r["pitch"] or r["label"])[:22], r["preview"]["secs"],
                     r["diversity"]["min_n"],
                     "OK" if r["diversity"]["min_n"] >= 2 else "!! 与某候选太像"))
        # 审美打分：**默认不算**（要 --critic）。它是"四层 QA 之外"的一条参考线，
        # 不进 proposal.json（那是契约，审美分不是契约的一部分），也不阻断任何流程。
        if critic:
            _print_critic(rows)
        if with_audio:
            print()
            print("   样音（12 秒 / 48 kHz 立体声）:")
            for r in rows:
                pv = r["preview"]
                if pv.get("audio"):
                    print("     %s  %-9s 生成 %4.2fs  %s"
                          % (r["id"], pv.get("audio_engine") or "?",
                             pv.get("audio_secs") or 0.0, pv["audio"]))
                else:
                    print("     %s  没生成：%s" % (r["id"], pv.get("audio_error") or "未知原因"))
        if unmatched:
            print()
            print("   ! 这些要求没生效（词表里没有，采样器没有照做）：%s"
                  % "、".join("「%s」" % w for w in unmatched))
        print()
        print("   联系表（并排对比）: %s" % compare)
        print("   确认其中一个: python propose.py --confirm --candidate %s" % rows[0]["id"])
        print("   都不满意:     python propose.py --revise \"你的要求\"")
    else:
        # 机读调用（CI / 上层脚本）拿不到任何东西的话，--json 就只是"关掉人读输出"，
        # 这跟参数名承诺的不是一回事；payload 本身就是这一版提案的全部事实。
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return payload


def _write_compare_sheet(rows, outdir):
    """把 N 个候选的联系表纵向拼成一张对比图（人一眼能看全）。"""
    try:
        from PIL import Image
    except ImportError:
        return "（没装 Pillow，跳过并排对比图）"
    paths = [r["preview"]["sheet"] for r in rows if r.get("preview", {}).get("sheet")]
    paths = [p for p in paths if p and os.path.exists(p)]
    if len(paths) < 2:
        return "（候选联系表不足 2 张，跳过并排对比图）"
    from PIL import ImageDraw
    # 中文标签必须用项目自己的字体链（PIL 默认位图字体没有中文字形，会渲成方块）。
    try:
        import filmkit as _K
        label_font = _K.font(19, "reg", "sans")
    except Exception:                                       # noqa: BLE001
        label_font = None
    ims = [Image.open(p).convert("RGB") for p in paths]
    tagged = [r for r in rows if r.get("preview", {}).get("sheet") in paths]
    w = max(i.size[0] for i in ims)
    band = 34   # 每行顶部加一条标签带：不标出来就看不出哪一行是哪个候选
    h = sum(i.size[1] for i in ims) + band * len(ims)
    sheet = Image.new("RGB", (w, h), (12, 12, 14))
    draw = ImageDraw.Draw(sheet)
    y = 0
    for im, r in zip(ims, tagged):
        draw.text((16, y + 7), "%s  %s  —  %s"
                  % (r.get("id"), r.get("label") or "", (r.get("pitch") or "")[:60]),
                  fill=(226, 232, 240), font=label_font)
        y += band
        sheet.paste(im, (0, y))
        y += im.size[1]
    out = os.path.join(outdir, "_compare.png")
    sheet.save(out)
    return out


# ---------------------------------------------------------------- 手改后重新绑定
def adopt(work, script=None, script_path="", by="user", note="", json_out=False):
    """把手改过的 work/design.json 重新绑成 confirmed（不动候选、不重采样）。

    为什么需要它：design.json 本来就是给人改的（配色/节奏/字幕都是开放字段），但
    proposal.json 存的是**确认那一刻**的设计指纹。改完再跑 render，提案闸判 stale 并在
    QC 报告里留一条 WARN——用户既没换方案也没换剧本，只是调了个颜色，却得重跑一遍
    propose 才能消掉警告，而重跑会把他刚改的东西覆盖掉。

    adopt 的语义是「现在这份 design.json 就是我要的那一版」：只重算指纹，
    不改 candidates、不改 chosen；指纹与提案记录不一致时说清是「手改」还是「选错文件」。
    """
    design_path = os.path.join(str(work), "design.json")
    if not os.path.isfile(design_path):
        raise SystemExit("[proposal] --adopt 需要 %s（先用 --confirm 冻结一份设计）" % design_path)
    design = _load_json(design_path)
    if not isinstance(design, dict) or not isinstance(design.get("visual"), dict):
        raise SystemExit("[proposal] %s 不像一份 design（缺 visual）" % design_path)
    designkit = _designkit()
    bad = designkit.validate_design(design)
    if bad:
        raise SystemExit("[proposal] %s 没过校验，不能 adopt：\n  - %s"
                         % (design_path, "\n  - ".join(bad[:5])))
    prev = _load_json(proposal_path(work), default=None)
    if not prev:
        raise SystemExit("[proposal] 没有 %s——先跑一次提案" % proposal_path(work))
    # 状态机：adopt 的语义是"把已确认的提案重新绑到这份手改过的 design.json"，不是"确认"。
    # 草案（draft、没有 chosen）走这里会写出 status=confirmed 但没有 chosen 的 proposal：
    # proposal_state 仍判 draft（闸不放行），adopt 却打印"闸不会再报 stale"——假绿。
    if str(prev.get("status") or "") != "confirmed" or not prev.get("chosen"):
        raise SystemExit(
            "[proposal] %s 还没确认过（status=%r，chosen=%r）——--adopt 只能把"
            "手改过的 work/design.json 重新绑到**已确认**的提案上。\n"
            "  先确认一个候选：python propose.py --work %s --confirm --candidate A"
            % (proposal_path(work), prev.get("status"), prev.get("chosen"), work))
    fp_new = designkit.design_fingerprint(design)
    fp_old = str(prev.get("designHash") or "")
    cands = {str(c.get("id")): c for c in (prev.get("candidates") or [])}
    row = cands.get(str(prev.get("chosen") or "")) or {}
    prev["status"] = "confirmed"
    prev["designHash"] = fp_new
    prev["adoptedAt"] = _now()
    prev["adoptedBy"] = str(by or "user")
    prev["adoptNote"] = str(note or "")
    if isinstance(script, dict) and script.get("lines"):
        prev["scriptHash"] = _sha(script)
        prev["scriptPath"] = os.path.abspath(str(script_path)) if script_path else ""
    _atomic_write(proposal_path(work), json.dumps(prev, ensure_ascii=False, indent=1))
    if json_out:
        print(json.dumps({"ok": True, "designHash": fp_new,
                          "path": proposal_path(work)}, ensure_ascii=False))
        return prev
    print("== adopt：把 work/design.json 重新绑成 confirmed")
    print("   新指纹: %s" % fp_new)
    if fp_old and fp_old != fp_new:
        why = ("手改过 design.json（符合预期）" if row.get("design_fp") == fp_old
               else "与提案里的候选指纹也不一致，确认一下改的是这份")
        print("   旧指纹: %s（%s）" % (fp_old, why))
    print("   已更新: %s" % proposal_path(work))
    print("   现在可以: python render.py --stage frames（提案闸不会再报 stale）")
    return prev

# ---------------------------------------------------------------- 确认 / 修订
def confirm(work, candidate, by="user", note="", json_out=False,
            script=None, script_path=""):
    prev = _load_json(proposal_path(work), default=None)
    if not prev:
        raise SystemExit("[proposal] 没有 %s——先跑一次提案" % proposal_path(work))
    cands = {str(c.get("id")): c for c in (prev.get("candidates") or [])}
    cid = str(candidate or "").strip().upper()
    if cid not in cands:
        raise SystemExit("[proposal] 没有候选 %r；本版有：%s"
                         % (candidate, " / ".join(sorted(cands))))
    design = cands[cid]["design"]
    designkit = _designkit()
    bad = designkit.validate_design(design)
    if bad:
        raise SystemExit("[proposal] 候选 %s 的设计没过校验，不能确认：\n  - %s"
                         % (cid, "\n  - ".join(bad)))
    prev["status"] = "confirmed"
    prev["chosen"] = cid
    prev["confirmedAt"] = _now()
    prev["confirmedBy"] = str(by or "user")
    prev["confirmNote"] = str(note or "")
    prev["designHash"] = designkit.design_fingerprint(design)
    # 只绑设计不够：设计没变而剧本大改，断句/时长/字幕全变，那次确认等于作废。
    # 只有调用方**显式**把剧本递进来才绑；没剧本可绑时必须清掉旧值，
    # 否则旧版本留下的指纹会在下次提案后误判成 stale。
    if isinstance(script, dict) and script.get("lines"):
        prev["scriptHash"] = _sha(script)
        prev["scriptPath"] = os.path.abspath(str(script_path)) if script_path else ""
    else:
        prev.pop("scriptHash", None)
        prev.pop("scriptPath", None)
    _atomic_write(proposal_path(work), json.dumps(prev, ensure_ascii=False, indent=1))
    _atomic_write(os.path.join(str(work), "design.json"),
                  json.dumps(design, ensure_ascii=False, indent=1))
    # 片间去重台账：**确认**（= 真的要出片了）才记，草稿候选不记。
    # 记的是设计本身（自足 JSON），不是路径；同一指纹只留最新一条。
    if ledger_path() and not ledger_record(design, work=work,
                                           label=cands[cid].get("label") or ""):
        print("   ! 片间去重台账没写成（%s）：本次确认照样生效，但下次提案不会避开这部片"
              % ledger_path(), file=sys.stderr)
    if json_out:
        # --json 的契约是 stdout 只输出 JSON：以前这里什么都不打，机读端拿到空 stdout，
        # json.loads 直接抛错——"确认成功"反而看起来像失败。
        print(json.dumps({"ok": True, "candidate": cid, "status": prev.get("status"),
                          "designHash": prev.get("designHash"),
                          "scriptHash": prev.get("scriptHash"),
                          "path": proposal_path(work),
                          "design": os.path.join(str(work), "design.json")},
                         ensure_ascii=False))
        return prev
    if not json_out:
        print("== 已确认候选 %s（%s）" % (cid, cands[cid].get("label")))
        if prev.get("scriptHash"):
            print("   剧本指纹: %s（剧本改了要重新提案）" % prev["scriptHash"])
        print("   冻结设计: %s" % os.path.join(str(work), "design.json"))
        # hybrid 的下一步不是"直接 render"：先把图文段的配色锚到素材，否则两段是两部片。
        _mode = ((_load_json(os.path.join(str(work), "intake.json"), default=None) or {})
                 .get("mode") or {}).get("resolved")
        if _mode == "hybrid":
            print("   下一步（hybrid 形态 3，默认）: 本设计只对**小面积叠加层**有用；"
                  "出片走 footage.py --stage all -> overlay.json -> compose.py --stage all")
            print("   形态 1（按段拼接）才需要 matchstyle：python matchstyle.py "
                  "--footage footage.json --work %s --apply --align-bars" % work)
        else:
            print("   现在可以: python render.py --script <film.json> --stage all")
    return prev

def revise(work, feedback, script_path, n=3, seed=None, n_frames=4, json_out=False,
           with_audio=False, no_ledger=False, no_intake=False, critic=False):
    """不满意就重出：新 rev 不覆盖旧 rev，feedback 原文留存（这就是"迭代"的审计）。"""
    if not str(feedback or "").strip():
        raise SystemExit("[proposal] --revise 需要一段要求（例如：更冷一点、女声、节奏快些）")
    prev = _load_json(proposal_path(work), default=None)
    if not prev:
        raise SystemExit("[proposal] 没有提案可改——先跑一次提案")
    script = _load_script(script_path)
    return make_proposal(str(feedback), script, work, n=n, seed=seed,
                         n_frames=n_frames, json_out=json_out,
                         with_audio=with_audio, no_ledger=no_ledger,
                         no_intake=no_intake, critic=critic)


def diff(work, rev_a, rev_b, json_out=False):
    dfk = _designkit()
    pa = _load_json(os.path.join(str(work), PROPOSALS_DIR, "rev%d" % int(rev_a), "proposal.json"),
                    default=None)
    pb = _load_json(os.path.join(str(work), PROPOSALS_DIR, "rev%d" % int(rev_b), "proposal.json"),
                    default=None)
    if not pa or not pb:
        raise SystemExit("[proposal] 找不到 rev%d 或 rev%d 的快照（work/%s/rev*/proposal.json）"
                         % (rev_a, rev_b, PROPOSALS_DIR))
    out = {"revA": int(rev_a), "revB": int(rev_b), "pairs": []}
    for ca in pa.get("candidates") or []:
        row = {"id": ca.get("id"), "label": ca.get("label"), "vs": []}
        for cb in pb.get("candidates") or []:
            d = dfk.design_distance(ca["design"], cb["design"])
            row["vs"].append({"id": cb.get("id"), "n": d.get("n", 0),
                              "axes": d.get("axes") or []})
        out["pairs"].append(row)
    if json_out:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return out
    print("== rev%d vs rev%d 的设计差异（n = 不同的设计维度数）" % (int(rev_a), int(rev_b)))
    for row in out["pairs"]:
        cells = "  ".join("%s:n=%d" % (v["id"], v["n"]) for v in row["vs"])
        print("   %s %-20s %s" % (row["id"], (row["label"] or "")[:20], cells))
    return out


def show(work, which="", json_out=False):
    prev = _load_json(proposal_path(work), default=None)
    if not prev:
        raise SystemExit("[proposal] 没有提案：%s" % proposal_path(work))
    if json_out:
        print(json.dumps(prev, ensure_ascii=False, indent=1))
        return prev
    print("== 提案 rev%d  status=%s  chosen=%s  时间=%s"
          % (prev.get("rev"), prev.get("status"), prev.get("chosen"), prev.get("at")))
    print("   brief: %s" % (prev.get("brief") or {}).get("topic"))
    if prev.get("scriptHash"):
        print("   剧本指纹: %s" % prev["scriptHash"])
    if prev.get("feedback"):
        print("   feedback: %s" % prev["feedback"])
    print()
    for r in prev.get("candidates") or []:
        if which and str(r.get("id")).upper() != str(which).upper():
            continue
        print("   [%s] %s" % (r.get("id"), r.get("pitch") or r.get("label")))
        print("       设计: %s" % (r.get("design_fp") or ""))
        print("       静帧: %s (%s 帧, %.2fs)"
              % ((r.get("preview") or {}).get("sheet"),
                 len((r.get("preview") or {}).get("frames") or []),
                 (r.get("preview") or {}).get("secs") or 0))
        pv = r.get("preview") or {}
        if pv.get("audio"):
            print("       样音: %s (%s, 生成 %.2fs)"
                  % (pv["audio"], pv.get("audio_engine") or "?", pv.get("audio_secs") or 0))
        elif pv.get("audio_error"):
            print("       样音: 没生成（%s）" % pv["audio_error"])
        div = r.get("diversity") or {}
        if div.get("vs"):
            print("       与其它候选: %s"
                  % ", ".join("%s(n=%d)" % (v["id"], v["n"]) for v in div["vs"]))
    return prev


# ---------------------------------------------------------------- 门禁接口（给 render.py 用）
def proposal_state(work, script_path=None):
    """给 render.py 的一行判定：返回 (status, detail)。

    status ∈ {"absent", "draft", "confirmed", "stale", "broken"}
      absent    —— 没提案（未启用，老项目就是这个）
      draft     —— 有提案但还没确认
      confirmed —— 已确认，且 design.json 与确认时的 designHash 一致、剧本也没换
      stale     —— 确认过，但 design.json 被改过（指纹对不上），或剧本改过了
      broken    —— 文件在但坏了

    script_path 可省：省略时按 confirm 时记下的 scriptPath 找，再退到 work/film.json。
    找不到/读不出剧本就**不判**剧本这一档（见 _script_hash_of），只判设计指纹。
    """
    p = proposal_path(work)
    try:
        data = _load_json(p, default=None)
    except SystemExit as e:
        # 坏 JSON 必须判 broken（文档里就是这一档）。以前把 SystemExit 直接抛给调用方：
        # render 会兜住，但任何直接调 proposal_state 的脚本会当场退出而不是拿到状态。
        return "broken", str(e)
    except OSError as e:
        return "broken", "读不出 %s：%s" % (p, e)
    if data is None:
        return "absent", "没有 %s" % p
    if not isinstance(data, dict):
        return "broken", "%s 不是 JSON 对象" % p
    if data.get("status") != "confirmed" or not data.get("chosen"):
        return "draft", "rev%s 还没确认（chosen=%r）" % (data.get("rev"), data.get("chosen"))
    try:
        design = _load_json(os.path.join(str(work), "design.json"), default=None)
    except SystemExit as e:
        return "broken", str(e)
    except OSError as e:
        return "broken", "读不出 work/design.json：%s" % e
    if design is None:
        return "stale", "confirmed 但 design.json 不存在（重新 --confirm）"
    try:
        dfk = _designkit()
        got = dfk.design_fingerprint(design)
    except SystemExit:
        raise
    if str(data.get("designHash") or "") != str(got):
        return "stale", "design.json 的指纹 %s 与确认时 %s 不一致" % (
            got, data.get("designHash"))

    # 剧本这一档只在确认时显式传过 script 才存在（scriptHash 有值才判）。
    sh = str(data.get("scriptHash") or "")
    if sh:
        cand_path = str(script_path or data.get("scriptPath") or "")
        if not cand_path:
            fallback = os.path.join(str(work), "film.json")
            cand_path = fallback if os.path.isfile(fallback) else ""
        now = _script_hash_of(cand_path)
        if now is not None and now != sh:
            return "stale", ("剧本改过了（%s 的指纹 %s -> %s），需重新提案"
                             % (cand_path, sh, now))
    return "confirmed", "候选 %s（rev%d）" % (data.get("chosen"), data.get("rev"))


# ---------------------------------------------------------------- CLI
def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="video-studio 提案器：先出方案，确认后再出片")
    ap.add_argument("--work", default="work")
    ap.add_argument("--layout", default="", choices=("", "landscape", "portrait"),
                    help="画布布局：landscape=1920x1080（默认）；portrait=1080x1920"
                         "（experimental：版式骨架与 compare/process/chart 已竖屏原生，"
                         "排版节奏未按竖屏复核）。必须与 film.json 的 video 块宽高比一致")
    ap.add_argument("--script", default="film.json")
    ap.add_argument("--brief", default="", help="一句话题材/要求（首次提案用）")
    ap.add_argument("--n", type=int, default=3, help="候选数（默认 3，最少 2）")
    ap.add_argument("--critic", action="store_true",
                    help="附带审美打分（离线启发式 0-100，只用于排序候选，不写 proposal.json）")
    ap.add_argument("--seed", default=None,
                    help="采样种子：给了就逐字节可复现（调试/回归用）；"
                         "不给则每次都不同（默认，满足『每条视频都不一样』）")
    ap.add_argument("--n-frames", type=int, default=4, help="每个候选渲几张静帧")
    ap.add_argument("--list", action="store_true", help="列出当前提案")
    ap.add_argument("--show", nargs="?", const="", default=None, help="看单个候选（给候选 id）")
    ap.add_argument("--confirm", action="store_true", help="确认某个候选（配 --candidate）")
    ap.add_argument("--adopt", action="store_true",
                    help="把手改过的 work/design.json 重新绑成 confirmed（只改指纹，不动候选）")
    ap.add_argument("--candidate", default="", help="要确认的候选 id（A/B/C…）")
    ap.add_argument("--by", default="user")
    ap.add_argument("--note", default="")
    ap.add_argument("--revise", default=None, help="带一段要求重出方案（新 rev，不覆盖旧的）")
    ap.add_argument("--diff", nargs=2, type=int, metavar=("REV_A", "REV_B"),
                    help="看两版设计的差异")
    ap.add_argument("--with-audio", action="store_true",
                    help="给每个候选附一段 12 秒样音（离线；配乐由 musicgen 生成）")
    ap.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    ap.add_argument("--no-ledger", action="store_true",
                    help="忽略片间去重台账（不读也不写；默认开启，路径见 --show-ledger）")
    ap.add_argument("--no-intake", action="store_true",
                    help="跳过首次提案前的问诊闸（会记进 proposal.intake 留痕）")
    ap.add_argument("--show-ledger", action="store_true",
                    help="打印台账路径与最近几条已交付设计（排查为什么这次避开了某方向）")
    a = ap.parse_args(argv)

    try:
        if a.show_ledger:
            path = ledger_path()
            rows = ledger_load(limit=20)
            print("[台账] %s" % (path or "已关闭（--no-ledger / VIDEO_STUDIO_LEDGER=off）"))
            for r in rows:
                print("  %s  %-22s  %s" % (r.get("at") or "", (r.get("label") or "")[:22],
                                           r.get("fingerprint") or ""))
            print("  共 %d 条（最近 20 条）" % len(rows))
            return 0
        if a.adopt:
            script = None
            try:
                if os.path.isfile(a.script):
                    script = _load_script(a.script)
            except SystemExit:
                script = None
            adopt(a.work, script=script, script_path=a.script, by=a.by,
                  note=a.note, json_out=a.json)
            return 0
        if a.diff:
            diff(a.work, a.diff[0], a.diff[1], json_out=a.json)
            return 0
        if a.confirm:
            if not a.candidate:
                raise SystemExit("[proposal] --confirm 需要 --candidate A/B/C")
            # 剧本存在才绑指纹：--confirm 不该因为 film.json 写坏或不在就失败，
            # 但能绑就必须绑，否则"设计没变、剧本改了"会被当成已确认。
            script, spath = None, ""
            try:
                if os.path.isfile(a.script):
                    script, spath = _load_script(a.script), a.script
            except SystemExit:
                script, spath = None, ""
            confirm(a.work, a.candidate, by=a.by, note=a.note, json_out=a.json,
                    script=script, script_path=spath)
            return 0
        if a.show is not None:
            show(a.work, a.show, json_out=a.json)
            return 0
        if a.list:
            show(a.work, "", json_out=a.json)
            return 0
        if a.revise is not None:
            revise(a.work, a.revise, a.script, n=a.n, seed=a.seed,
                   n_frames=a.n_frames, json_out=a.json,
                   with_audio=a.with_audio, no_ledger=a.no_ledger,
                   no_intake=a.no_intake, critic=a.critic)
            return 0
        if a.brief or os.path.exists(a.script):
            script = _load_script(a.script)
            make_proposal(a.brief, script, a.work, n=a.n, seed=a.seed,
                          n_frames=a.n_frames, json_out=a.json,
                          with_audio=a.with_audio, no_ledger=a.no_ledger,
                          no_intake=a.no_intake, critic=a.critic)
            return 0
        ap.print_help()
        return 0
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit("[proposal] %s: %s" % (type(e).__name__, e)) from e


if __name__ == "__main__":
    sys.exit(main())
