# -*- coding: utf-8 -*-
"""gates.py — 门禁账本 / 机读交付报告 / 人工签核。

解决三件事（都是"文档承诺了、代码没做"）：

1) **门禁账本**：每个 stage 把判据结果记成一条 {id, title, status, detail}，
   status 只允许 pass / fail / skipped 三档。"没执行"必须独立成 skipped，
   不许混进 pass——否则"看起来干净、其实没在查"会一路带进交付。
2) **机读交付报告** out/QC-REPORT.md：把账本渲染成 gate matrix + 实测值 +
   人工签核 + 耗时台账。在此之前，质检结论只活在 stdout 里，会话一断就没了。
3) **人工签核** work/signoff.json：风格/母版/事实/交付四道"必须由人看"的闸。
   签核绑 contentHash（剧本 + scenes.py + 引擎），内容一变旧签核自动失效。

设计约束：
  * 纯标准库（hashlib/json/time/os），不引第三方。
  * 账本与签核写 work/ 下，报告写 out/——渲染产物可整目录搬走。
  * 不改变既有退出码语义：默认 skipped 只告警，--strict-gates 才升级为 error
    （与 QA_REQUIRE_SYNC 同一套语义）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

SCHEMA = 1

# 人工签核的四道闸：id -> 人话标题
GATES = {
    "style": "风格与声线已确认（看 work/style.json / 试听声线）",
    "stills": "母版静帧已亲眼看过（work/qa/sheet.png）",
    "facts": "上屏数字已核对出处（FACTS.md）",
    "release": "成片已验收（音画 / 字幕 / 响度）",
}
GATE_ORDER = ("style", "stills", "facts", "release")

OK, FAIL, SKIP = "pass", "fail", "skipped"


def _sha1_text(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:16]


def _sha1_file(path):
    """文件内容指纹；缺文件返回 'missing'，不抛异常。"""
    try:
        h = hashlib.sha1()
        with open(str(path), "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()[:16]
    except OSError:
        return "missing"


WEB_ASSET_EXT = (".js", ".mjs", ".html")
# web 引擎的画面逻辑在页面脚本里，不在 scenes.py 里。这几个后缀按文件名排序后逐个取
# sha1，再合并成一个指纹；不含 .css（样式改动不影响"画面语义"，也确实常见于调色试错）。


def web_asset_fingerprint(entry):
    """入口页面同目录下 .js/.mjs/.html 的内容指纹（按文件名排序后拼接）。

    返回 (combined, files)：combined 是 16 位 hex（目录读不了返回 "missing"）；
    files 是参与计算的 [文件名]，只用于报告/排错，不进指纹（避免绝对路径进 hash）。
    """
    if not entry:
        return "missing", []
    d = os.path.dirname(os.path.abspath(str(entry)))
    try:
        names = sorted(n for n in os.listdir(d) if n.lower().endswith(WEB_ASSET_EXT))
    except OSError:
        return "missing", []
    if not names:
        return "missing", []
    rows = []
    for n in names:
        rows.append("%s:%s" % (n, _sha1_file(os.path.join(d, n))))
    return _sha1_text("\n".join(rows)), names


def content_hash(script=None, scenes_path="", script_path="", engine="pil", web_entry="",
                 work=""):
    """本次渲染的"内容身份"：剧本 + 场景代码 + 冻结设计 + 生成设计的引擎 + 引擎（web 时再加页面脚本）。

    只取与"这一版画面/声音"相关的量：
      * 剧本：去掉渲染期注入的 _path/_dir/_style 等私有键后规范化 json；
      * scenes.py：文件内容（画面逻辑变了，旧签核就得失效）；
      * design（work/design.json，存在才算）：冻结设计是"这一版长什么样"的唯一来源
        （propose.py --confirm 写出，designkit.design_to_plan 解析）。**只改配色
        visual.palette / 版式几何**也能让画面全变——不纳入的话旧签核继续有效，
        正是"内容变了旧签核自动失效"这条承诺的反例。按**文件字节** sha1，不重新
        json 规范化：规范化会把"字节不同但语义相同"的重写算成同一版，而写盘口径
        （propose.py 的输出）本身就是权威。design 不存在时省略该键（没确认过提案的
        legacy 项目不该被判"内容已变"）。
      * designkit / stylekit / grammar：生成与解析设计的源码（键名分开）。同一个
        design.json 在这三个模块改动后可能解析出不同计划，旧签核同样必须失效。
        注意**不含** filmkit.py：那是渲染内核，把它算进去会让每次技能升级都判
        "内容已变"，签核无故失效（与 web_assets 只算页面脚本同一条取舍）。
      * engine：pil 与 web 是两条渲染路径，产物不可互换；
      * web_entry：engine=web 时，入口页同目录的 .js/.mjs/.html 一起算。
        **pil 路线不参与**——否则老 PIL 项目每次升级都会被判"内容已变"，签核无故失效。
    """
    body = {}
    if isinstance(script, dict):
        for k, v in script.items():
            if str(k).startswith("_"):
                continue
            body[k] = v
    eng = str(engine or "pil")
    payload = {
        "script": _sha1_text(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)),
        "script_file": _sha1_file(script_path) if script_path else "",
        "scenes": _sha1_file(scenes_path) if scenes_path else "",
        "engine": eng,
    }
    design_path = os.path.join(str(work), "design.json") if work else ""
    if design_path and os.path.exists(design_path):
        payload["design"] = _sha1_file(design_path)
    here = os.path.dirname(os.path.abspath(__file__))
    payload["design_engine"] = {name: _sha1_file(os.path.join(here, name + ".py"))
                                for name in ("designkit", "stylekit", "grammar")}
    if eng == "web":
        assets, _names = web_asset_fingerprint(web_entry)
        payload["web_assets"] = assets
    return _sha1_text(json.dumps(payload, sort_keys=True))


class GateLedger:
    """一次运行内的门禁账本（多 stage 共用；--stage all 时是同一个对象）。"""

    def __init__(self, work, script=None, scenes_path="", script_path="", engine="pil",
                 web_entry="", run_id=""):
        self.work = str(work or "work")
        self.items = []
        self.t0 = time.time()
        # run_id 是"这一版"的标签（--run-id）。没给就按时间戳生成，
        # 保证每次运行在 work/runs/<runId>/ 下都有独立记录，而不是互相覆盖。
        self.run_id = str(run_id or "").strip() or time.strftime("r%Y%m%d-%H%M%S")
        self.script = script or {}
        self.scenes_path = str(scenes_path or "")
        self.script_path = str(script_path or "")
        self.engine = str(engine or "pil")
        self.web_entry = str(web_entry or "")
        self.web_assets = []
        # work 必须一起传：冻结设计在 work/design.json 里，内容指纹要包含它。
        self.content_hash = content_hash(script, self.scenes_path, self.script_path,
                                         self.engine, web_entry=self.web_entry,
                                         work=self.work)
        if self.engine == "web":
            _fp, self.web_assets = web_asset_fingerprint(self.web_entry)
        self.bypassed = []          # --no-gate 绕过的记录（必须留痕）

    # ---- 记账 ----
    def add(self, gid, title, status, detail="", at=None):
        """记一条门禁。同一个 gid 重复记时**后写覆盖**（stage 可重跑）。"""
        if status not in (OK, FAIL, SKIP):
            raise ValueError("门禁状态只能是 pass/fail/skipped，收到 %r" % (status,))
        item = dict(id=str(gid), title=str(title), status=status,
                    detail=str(detail or ""), at=float(at if at is not None else time.time()))
        for i, old in enumerate(self.items):
            if old["id"] == item["id"]:
                self.items[i] = item
                return item
        self.items.append(item)
        return item

    def ok(self, gid, title, detail=""):
        return self.add(gid, title, OK, detail)

    def fail(self, gid, title, detail=""):
        return self.add(gid, title, FAIL, detail)

    def skip(self, gid, title, detail=""):
        return self.add(gid, title, SKIP, detail)

    def bypass(self, what, why=""):
        self.bypassed.append(dict(what=str(what), why=str(why or ""), at=time.time()))

    # ---- 判定 ----
    def fails(self):
        return [x for x in self.items if x["status"] == FAIL]

    def skips(self):
        return [x for x in self.items if x["status"] == SKIP]

    def counts(self):
        c = {OK: 0, FAIL: 0, SKIP: 0}
        for x in self.items:
            c[x["status"]] = c.get(x["status"], 0) + 1
        return c

    def exit_errors(self, strict=False):
        """该让命令失败吗：任何 fail 都失败；--strict-gates 下 skipped 也算失败。"""
        errs = ["[%s] %s %s" % (x["id"], x["title"],
                                ("— " + x["detail"]) if x["detail"] else "")
                for x in self.items if x["status"] == FAIL]
        if strict:
            errs += ["[%s] %s：本次**没有执行**（--strict-gates 下算失败）"
                     % (x["id"], x["title"]) for x in self.items if x["status"] == SKIP]
        return errs

    # ---- 落盘 ----
    def to_dict(self):
        d = dict(schema=SCHEMA, at=self.t0, contentHash=self.content_hash,
                 runId=self.run_id,
                 engine=self.engine, counts=self.counts(),
                 items=list(self.items), bypassed=list(self.bypassed),
                 inputs=self.input_fingerprints())
        if self.engine == "web":
            # 审计用：让用户/CI 能看出这个指纹到底盖了哪些页面脚本（不进 hash，绝对路径不外泄）
            d["webAssets"] = list(self.web_assets)
        return d

    def input_fingerprints(self):
        """输入指纹快照：剧本 / scenes / 词级 / 本脚本。

        为什么要单独记：contentHash 是"一个 16 位 hex"，能判"变了没"，但**说不出
        变了哪个**。批量出片时最常见的问题是"这条用的还是上一版的词级时间戳"，
        有个逐项指纹才能在事后对账（也让 QT 报告能标出"本次跑的是哪一版输入"）。
        """
        return {
            "script": _sha1_file(self.script_path) if self.script_path else "missing",
            "scenes": _sha1_file(self.scenes_path) if self.scenes_path else "missing",
            "words": _sha1_file(os.path.join(self.work, "words.json")),
            # 审计用：设计改了 contentHash 会变，但说不清"变的是哪一项"。这里单记一条。
            "design": _sha1_file(os.path.join(self.work, "design.json")),
            # 键名必须与文件一致：以前叫 gates_py 却哈希 filmkit.py，事后对账会拿着
            # "gates.py 的指纹"去比另一个文件。这里只做审计（不进 contentHash）。
            "filmkit": _sha1_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "filmkit.py")),
        }

    def write_json(self):
        """work/gates.json —— 给程序读（QC-REPORT.md 只是它的渲染层）。"""
        os.makedirs(self.work, exist_ok=True)
        p = os.path.join(self.work, "gates.json")
        d = self.to_dict()
        d["wall_s"] = round(time.time() - self.t0, 2)
        _atomic_json(p, d)
        return p


def archive_run(work, ledger, artifacts=None, report_path=None):
    """把"这一版"的账本快照归档到 work/runs/<runId>/。

    只归档**小文件**（gates.json / QC-REPORT.md / run.json）。视频/音频/分块体积大，
    就地保留即可——这里要解决的是"跑了几版之后，谁都说不清上一版的门禁结论是什么"，
    不是备份媒体。返回 run 目录。
    """
    d = os.path.join(str(work), "runs", str(ledger.run_id))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:                                    # noqa: BLE001
        return "", "建目录失败：%s" % e
    info = dict(runId=ledger.run_id, at=ledger.t0,
                atText=time.strftime("%Y-%m-%d %H:%M:%S"),
                contentHash=ledger.content_hash, counts=ledger.counts(),
                inputs=ledger.input_fingerprints(),
                artifacts=[str(x) for x in (artifacts or [])])
    _atomic_json(os.path.join(d, "run.json"), info)
    bad = []
    for src, name in ((os.path.join(str(work), "gates.json"), "gates.json"),
                      (str(report_path or ""), "QC-REPORT.md")):
        try:
            if src and os.path.exists(src):
                with open(src, "rb") as fh:
                    data = fh.read()
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(data)
        except OSError as e:
            # 归档失败必须让调用方看见：这版账本会丢，事后就查不清"上一版结论是什么"。
            # 以前 except OSError: pass —— 归档目录缺文件，神不知鬼不觉。
            bad.append("%s（%s）" % (name, e))
    if bad:
        return d, "归档不完整：%s" % "；".join(bad)
    return d, ""


# ---------------------------------------------------------------- 签核账本
def signoff_path(work):
    return os.path.join(str(work), "signoff.json")


def read_signoff(work):
    """读签核账本；坏 JSON 当作"没有签核"，但把原因带出来（不静默吞掉）。"""
    p = signoff_path(work)
    if not os.path.exists(p):
        # 只报 basename：交付凭据里写绝对路径，等于把作者机器的临时目录
        # （%TEMP%\vs-smoke-xxxx）抄进用户仓库的 out/QC-REPORT.md。
        return {"schema": SCHEMA, "signed": {}}, "没有 %s（还没人签核过）" % os.path.basename(p)
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"schema": SCHEMA, "signed": {}}, "%s 读不了（%s）——当作没有签核" % (os.path.basename(p), e)
    if not isinstance(d, dict) or not isinstance(d.get("signed"), dict):
        return {"schema": SCHEMA, "signed": {}}, "%s 结构不对——当作没有签核" % os.path.basename(p)
    return d, ""


def write_signoff(work, gid, content_hash, verdict="approved", by="", note="", artifacts=None):
    """写一条人工签核。verdict=approved / rejected；rejected 视为没签。"""
    if str(gid) not in GATES:
        raise ValueError("未知的门禁 id %r（可选：%s）" % (gid, " / ".join(GATE_ORDER)))
    d, _why = read_signoff(work)
    d.setdefault("schema", SCHEMA)
    d.setdefault("signed", {})
    d["signed"][str(gid)] = dict(
        verdict=str(verdict), at=time.time(),
        atText=time.strftime("%Y-%m-%d %H:%M:%S"), by=str(by or ""),
        contentHash=str(content_hash or ""), note=str(note or ""),
        artifacts=[str(x) for x in (artifacts or [])],
    )
    os.makedirs(str(work), exist_ok=True)
    _atomic_json(signoff_path(work), d)
    return d["signed"][str(gid)]


def signoff_state(work, current_hash):
    """返回 (每道闸的判定, 因内容变化而失效的闸列表)。

    判定只有三档：pass（签了且内容一致）/ fail（明确 rejected）/
    skipped（没签过，或内容已变——"这一版还没签"，不是"签坏了"）。
    """
    d, why = read_signoff(work)
    signed = (d.get("signed") or {}) if isinstance(d, dict) else {}
    state, stale = {}, []
    for gid in GATE_ORDER:
        rec = signed.get(gid)
        if not isinstance(rec, dict):
            state[gid] = dict(status=SKIP, detail=why or "没签核过")
            continue
        if str(rec.get("verdict")) != "approved":
            state[gid] = dict(status=FAIL, detail="签核结论是 %r" % rec.get("verdict"))
            continue
        rec_hash = str(rec.get("contentHash") or "")
        if not rec_hash:
            # 签核本身没有内容指纹（gates.json 不存在时 --accept 写下的），无法核对
            # 是否属于这一版内容。旧逻辑拿 "" == "" 当"内容一致"，于是没跑过任何门禁
            # 的 work 里这条签核会在 QC 报告里显示 pass。
            state[gid] = dict(status=SKIP,
                              detail="签核没有绑内容指纹（签核时没有 gates.json），"
                                     "无法核对属于哪一版内容；先跑 --stage qa 再签")
            continue
        if not str(current_hash or ""):
            # 当前版本算不出内容指纹（还没有 gates.json / 刚被删）：同样不能判通过。
            state[gid] = dict(status=SKIP,
                              detail="当前还没有内容指纹（没跑过产出 gates.json 的阶段），"
                                     "签核无法核对是否属于这一版；先跑 --stage qa")
            continue
        if str(rec.get("contentHash") or "") != str(current_hash):
            state[gid] = dict(status=SKIP,
                              detail="内容已变（签核时 %s / 现在 %s），需重新签"
                                     % (str(rec.get("contentHash"))[:8], str(current_hash)[:8]))
            stale.append(gid)
            continue
        bits = [str(rec.get("atText") or "")]
        if rec.get("by"):
            bits.append(str(rec["by"]))
        if rec.get("note"):
            bits.append(str(rec["note"]))
        state[gid] = dict(status=OK, detail=" 已签（%s）" % " · ".join(bits))
    return state, stale


# ---------------------------------------------------------------- 耗时台账
def append_cost(work, stage, wall_s, **extra):
    """work/cost.jsonl —— 每 stage 一行实测（排期公式从没被验证过，这是它的对照）。"""
    os.makedirs(str(work), exist_ok=True)
    row = dict(at=time.time(), atText=time.strftime("%Y-%m-%d %H:%M:%S"),
               stage=str(stage), wall_s=round(float(wall_s), 3))
    for k, v in extra.items():
        if v is not None:
            row[k] = v
    p = os.path.join(str(work), "cost.jsonl")
    # 写不进去要抛出去：render._stage_end 会接住并打 WARN。以前这里静默 pass，
    # 于是"--cost 开了但 cost.jsonl 一直没写"没有任何痕迹。
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return p


def read_cost(work):
    p = os.path.join(str(work), "cost.jsonl")
    rows = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


# ---------------------------------------------------------------- 报告
def _fmt_s(sec):
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "-"
    if sec < 60:
        return "%.1fs" % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return "%dm%02ds" % (int(m), int(s))
    h, m = divmod(m, 60)
    return "%dh%02dm" % (int(h), int(m))


def render_report(work, out=None):
    """把 work/gates.json + signoff + cost.jsonl 渲染成 out/QC-REPORT.md。"""
    gj = os.path.join(str(work), "gates.json")
    g = {}
    if os.path.exists(gj):
        try:
            g = json.load(open(gj, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            g = {}
    ch = str(g.get("contentHash") or "")
    items = list(g.get("items") or [])
    counts = {OK: 0, FAIL: 0, SKIP: 0}
    # 坏条目必须先归到"不通过"里，不能让它从三种计数里漏出去：
    # 以前 st 不是 pass/fail/skipped 时 counts 只加了个 count[x]，而结论分支只看
    # counts[FAIL] / counts[SKIP] —— 于是 "status": "warn"（或大小写写错、null）
    # 的条目在矩阵里照样列出来，结论却写"所有门禁本次都执行且通过"。
    # 这正是本模块开头要防的"看起来干净、其实没在查"，所以未知状态一律按 fail 计。
    bad_items = 0
    for x in items:
        if not isinstance(x, dict):
            bad_items += 1
            continue
        st = x.get("status")
        if st not in (OK, FAIL, SKIP):
            bad_items += 1
            counts[FAIL] += 1
            continue
        counts[st] += 1
    state, stale = signoff_state(work, ch)
    signed_n = sum(1 for v in state.values() if v["status"] == OK)

    L = []
    L.append("# QC 报告（成片交付凭据）")
    L.append("")
    L.append("> 本文件由 `render.py --stage qa` 自动生成，是**机器可读的交付结论**。")
    L.append("> 口径：`pass` = 本次真的查过且通过；`fail` = 查了没过；"
             "`skipped` = **本次没执行**（不等于通过）。")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 内容指纹 | `%s` |" % (ch or "未生成"))
    L.append("| 运行编号 | `%s` |" % (g.get("runId") or "-"))
    L.append("| 引擎 | `%s` |" % (g.get("engine") or "-"))
    _inp = g.get("inputs") or {}
    if _inp:
        L.append("| 输入指纹 | script `%s` · scenes `%s` · words `%s` |"
                 % (_inp.get("script", "-"), _inp.get("scenes", "-"),
                    _inp.get("words", "-")))
    L.append("| 生成时间 | %s |"
             % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(g.get("at") or time.time())))
    L.append("| 门禁合计 | pass %d · fail %d · skipped %d |"
             % (counts.get(OK, 0), counts.get(FAIL, 0), counts.get(SKIP, 0)))
    if bad_items:
        L.append("| 账本损坏条目 | %d 条（status 不是 pass/fail/skipped）——按 fail 计 |"
                 % bad_items)
    L.append("| 人工签核 | %d/%d 有效 |" % (signed_n, len(GATE_ORDER)))
    L.append("")
    L.append("## 一、门禁矩阵")
    L.append("")
    if items:
        L.append("| 门禁 | 判据 | 结果 | 实测 / 说明 |")
        L.append("|---|---|---|---|")
        for x in items:
            if not isinstance(x, dict):
                # 坏条目不能让整份报告炸掉（.get 会 AttributeError，_finish_gates 只
                # 打一行 WARN，交付目录里就没有 QC-REPORT.md 了）：照实列成一行。
                L.append("| `?` | （账本条目损坏） | **fail** | %r |"
                         % (str(x)[:60].replace("|", "\\|"),))
                continue
            L.append("| `%s` | %s | **%s** | %s |"
                     % (x.get("id", "?"), x.get("title", ""), x.get("status", "?"),
                        (x.get("detail") or "-").replace("|", "\\|")))
    else:
        L.append("（没有门禁记录：先跑 `--stage qa`）")
    L.append("")
    L.append("## 二、人工签核")
    L.append("")
    L.append("| 闸 | 要求 | 结果 | 说明 |")
    L.append("|---|---|---|---|")
    for gid in GATE_ORDER:
        v = state.get(gid, {})
        L.append("| `%s` | %s | **%s** | %s |"
                 % (gid, GATES[gid], v.get("status", "?"),
                    (v.get("detail") or "-").replace("|", "\\|")))
    if stale:
        L.append("")
        L.append("> 内容已变的签核：%s（改了剧本 / scenes.py / 引擎 / web 页面脚本，"
                 "旧签核自动失效，需重新签）"
                 % "、".join("`%s`" % x for x in stale))
    L.append("")
    L.append("## 三、耗时台账（`work/cost.jsonl` 实测，非估算）")
    L.append("")
    rows = read_cost(work)
    if rows:
        L.append("| stage | 墙钟 | 备注 |")
        L.append("|---|---|---|")
        total = 0.0
        for r in rows:
            total += float(r.get("wall_s") or 0)
            note = " · ".join("%s=%s" % (k, r[k])
                              for k in ("frames", "n_tts", "n_cues", "rework")
                              if r.get(k) not in (None, ""))
            L.append("| `%s` | %s | %s |"
                     % (r.get("stage", "?"), _fmt_s(r.get("wall_s")), note or "-"))
        L.append("| **合计** | **%s** | |" % _fmt_s(total))
    else:
        L.append("（还没有台账：首次运行或未接入 cost.jsonl）")
    L.append("")
    if g.get("bypassed"):
        L.append("## 四、本次绕过的门禁（留痕，不是通过）")
        L.append("")
        for b in g["bypassed"]:
            L.append("- %s%s" % (b.get("what"), ("：" + b["why"]) if b.get("why") else ""))
        L.append("")
        nxt = "五"
    else:
        nxt = "四"
    L.append("## %s、结论（照抄，别自己发挥）" % nxt)
    L.append("")
    if not items:
        # 没有门禁记录 ≠ 全部通过。以前这里落到 else 分支打印"**通过**"，
        # 于是 gates.json 被删 / 写坏 / 根本没跑 qa 时，QC-REPORT.md 会是一份
        # 宣称通过的交付凭据——正是本模块开头要防的"看起来干净、其实没在查"。
        if not os.path.exists(gj):
            why = "没有 %s（本次没有跑过任何 stage，或 work/ 目录已丢）" % gj
        else:
            why = "%s 读不出来 / 里面 0 条门禁（JSON 损坏或并发写坏）" % gj
        L.append("- **未判定**：%s。**这不等于通过**——先跑 --stage qa（或 --stage all）"
                 "产出真实门禁再交付。" % why)
    elif counts.get(FAIL) - bad_items > 0:
        # counts[FAIL] 里含坏条目（上面按 fail 计）。真 fail 与"账本坏了"必须分开说：
        # 只有坏条目时不能宣称"查了没过"，那会把人引去修不存在的判据问题。
        L.append("- **未通过**：fail %d 项，按上表逐条修，别宣称完成。"
                 % (counts.get(FAIL) - bad_items))
    elif bad_items:
        # 条目损坏 / 状态值不认识：既不能算"通过"，也不该说成"查了没过"。
        L.append("- **未判定**：账本里有 %d 条损坏的记录（不是合法 dict 或 status 不是 "
                 "pass/fail/skipped），无法判断这些门禁到底查没查、过没过。"
                 "**这不等于通过**——重跑 --stage qa 重新产出账本。" % bad_items)
    elif counts.get(SKIP):
        L.append("- **有条件通过**：skipped %d 项（明确没执行），交付时必须逐项说明原因。"
                 % counts.get(SKIP))
    else:
        L.append("- **通过**：所有门禁本次都执行且通过。")
    L.append("- 报告：`%s`；机器可读源：`%s`。"
             % (out or os.path.join("out", "QC-REPORT.md"), gj))
    L.append("")
    return "\n".join(L)


def write_report(work, out_path=None):
    p = str(out_path or os.path.join(os.path.dirname(str(work)) or ".", "out", "QC-REPORT.md"))
    d = os.path.dirname(os.path.abspath(p))
    if d:
        os.makedirs(d, exist_ok=True)
    text = render_report(work, out=p)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


# ---------------------------------------------------------------- 原子写
def atomic_write_json(path, obj):
    """先写 .tmp 再 os.replace：断电 / 被杀不会留下半截 JSON。"""
    d = os.path.dirname(os.path.abspath(str(path)))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (str(path), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, str(path))


def _atomic_json(path, obj):
    return atomic_write_json(path, obj)


def _setup_stdio():
    """重定向到管道/文件时切 UTF-8：Windows 默认代码页是 GBK，中文与 →/—/✓ 会乱码
    甚至 UnicodeEncodeError。与 render.py / studio.py 同一口径。

    只在 __main__ 里调用：本模块被 render.py import，模块级改 stdout 会污染调用方。
    """
    import sys as _sys
    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass


if __name__ == "__main__":
    import argparse
    import sys

    _setup_stdio()
    ap = argparse.ArgumentParser(description="门禁账本 / 签核 / QC 报告")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out", default="")
    ap.add_argument("--accept", default="",
                    help="签核一道闸：%s" % " / ".join(GATE_ORDER))
    ap.add_argument("--reject", default="", help="否决一道闸（同样记一条，但不算通过）")
    ap.add_argument("--note", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--artifacts", default="", help="逗号分隔的证据文件路径")
    ap.add_argument("--json", action="store_true", help="打印账本 JSON")
    a = ap.parse_args()
    try:
        if a.accept or a.reject:
            gid = (a.accept or a.reject).strip()
            d, _why = read_signoff(a.work)
            ch = ""
            gj = os.path.join(a.work, "gates.json")
            if os.path.exists(gj):
                try:
                    ch = str(json.load(open(gj, encoding="utf-8")).get("contentHash") or "")
                except (json.JSONDecodeError, OSError):
                    ch = ""
            rec = write_signoff(a.work, gid, ch,
                                verdict="approved" if a.accept else "rejected",
                                by=a.by, note=a.note,
                                artifacts=[x.strip() for x in a.artifacts.split(",") if x.strip()])
            print("[签核] %s = %s（内容指纹 %s）" % (gid, rec["verdict"], ch or "未知"))
            if not ch:
                # 没有 gates.json = 没跑过任何产出账本的阶段。空指纹签核不算"这一版签过"，
                # QC 报告里会记 skipped；这里先把话说在前面，别让人以为签完就生效了。
                print("[WARN] 没有可用的内容指纹（work/gates.json 不存在）：这条签核"
                      "**不会**在 QC 报告里算通过，先跑 --stage qa 再签")
        p = write_report(a.work, a.out or None)
        print("[QC 报告]", p)
        if a.json:
            # 读磁盘上的账本，别现场 new 一个空的：GateLedger(work) 不传剧本时
            # contentHash 是按空剧本算的，打出来的 JSON 跟 work/gates.json 对不上、
            # items 永远是 [] —— 机读输出等于撒谎。
            gp = os.path.join(str(a.work), "gates.json")
            try:
                with open(gp, encoding="utf-8") as fh:
                    print(json.dumps(json.load(fh), ensure_ascii=False, indent=1))
            except (OSError, json.JSONDecodeError) as err:
                print(json.dumps(dict(ok=False, work=str(a.work),
                                      error="没有可读的 %s：%s" % (gp, err)),
                                 ensure_ascii=False, indent=1))
        sys.exit(0)
    except (ValueError, OSError) as e:
        print("[ERROR] %s" % e, file=sys.stderr)
        sys.exit(2)
