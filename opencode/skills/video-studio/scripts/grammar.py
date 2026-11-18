# -*- coding: utf-8 -*-
"""grammar.py — 场景语法：这段内容该长成什么构图。

为什么单独一个模块
------------------
"叙事语气"（kind: hook/body/quote/emphasis/calm/outro）与"画面构图"
（scene type: hero/list/stat/compare/...）是两个**正交**维度：
  - kind 决定旁白声线、语气、配乐能量（voicekit / scorekit 读它）；
  - scene type 决定这段画面长什么样（scenes.py 的 _DRAWERS 读它）。
两者被混成一个 kind 时，scenes.py 只有 hook/body/outro 三个绘制器，
quote/emphasis/calm 全部回落到 body —— 画面因此只有三种骨架，换配色也换不掉"每屏一个样"。

把"内容 -> 构图"的选择逻辑独立出来，有三个好处：
  1. 纯函数、只依赖标准库：可以单测，不需要 PIL / TTS / 渲染；
  2. 选择理由落盘（work/storyboard.json），用户能看到"为什么这段画成数字卡"；
  3. scenes.py 只负责"怎么画"，不负责"画什么"——改构图不动选择逻辑，反之亦然。

铁律：**不许**用 random / time / hash()（str hash 每进程带随机盐）。
所有派生量只用 sha1 前 8 位，保证"同一份稿子永远同一张分镜表"。
"""
from __future__ import annotations

import hashlib
import re

# ============================================================================
# 一、10 类场景（顺序 = 平手时的固定取位顺序，改顺序会让同一稿子的分镜漂移）
# ============================================================================

SCENE_TYPES = ("hero", "chapter", "stat", "chart", "compare",
               "process", "quote", "list", "statement", "outro")

SCENE_LABELS = {
    "hero": "封面",
    "chapter": "章节卡",
    "stat": "数字卡",
    "chart": "数据条",
    "compare": "左右对比",
    "process": "流程轴",
    "quote": "引语",
    "list": "要点列表",
    "statement": "金句",
    "outro": "收尾",
}

# 最短段长（秒）：低于它动画演不完，必须按 DOWNGRADE 降级。
# 数字来源不是拍脑袋：入场动效本身 0.8~1.3s，条目 stagger 3 条约 0.9s，
# 留 1/3 屏时间给观众读，才撑得住。
MIN_DUR = {
    "hero": 2.4,
    "chapter": 2.2,
    "stat": 3.2,
    "chart": 4.5,
    "compare": 4.0,
    "process": 4.0,
    "quote": 4.0,
    "list": 3.0,
    "statement": 2.6,
    "outro": 2.4,
}

# 段太短时的降级目标（键必须都在 SCENE_TYPES 里）
DOWNGRADE = {
    "hero": "statement",
    "chapter": "statement",
    "stat": "statement",
    "chart": "list",
    "compare": "list",
    "process": "list",
    "quote": "statement",
    "list": "statement",
    "statement": "statement",
    "outro": "statement",
}

# 连续同类型最多几段（超出换下一个候选，防三连 list 的"复制粘贴感"）
MAX_REPEAT = 2

# ============================================================================
# 二、确定性哈希（sha1 前 8 位；禁 hash()：str hash 每次进程带随机盐）
# ============================================================================


def _digest(*parts):
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:8], 16)


def _hash01(*parts):
    """0..1 的稳定随机数（替代 random.random()）。"""
    return (_digest(*parts) % 100003) / 100003.0


def _hash_mod(n, *parts):
    n = max(1, int(n))
    return _digest(*parts) % n


# ============================================================================
# 三、内容特征（全部是"有没有这类信号"，不做语义理解）
# ============================================================================

# 对比词：出现即强烈暗示"左右对照"构图
COMPARE_WORDS = ("对比", "相比", "相比之", "优劣", "差异", "前后", "以前", "如今",
                 "vs", "VS", "而不是", "一边", "另一边", "传统", "新版")

# 顺序词：出现即暗示"流程/步骤"构图
SEQUENCE_WORDS = ("第一步", "第二步", "第三步", "首先", "然后", "接着", "最后",
                  "第一步", "步骤", "流程", "环节", "→", "->", "依次", "接下来")

# 引语标记：出现即暗示"引语"构图
QUOTE_MARKS = ("「", "」", "『", "』", "“", "”", "——", "说：", "写道", "认为",
               "曾说过", "有句话", "原话")

# 数字 + 单位：数字卡的原料。单位表刻意只收中文视频里真正常见的，
# 加太宽会把"2024 年"这种年份也当成数据（年份单独排除）。
_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|％|倍|万|亿|千万|百万|人|名|家|个|次|天|年|月|小时|分钟|秒|元|块|美元|GB|TB|MB|ms|℃|度)"
)
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _text_of(line):
    """把一段里所有可读文字摊平（标题权重最高，放最前）。"""
    chunks = []
    for k in ("big", "text", "sub", "kicker", "chapter"):
        v = line.get(k)
        if v:
            chunks.append(str(v))
    for it in line.get("items") or []:
        chunks.append(str(it.get("text") if isinstance(it, dict) else it))
    return "".join(chunks)


def _narrative_of(line):
    """只取"叙述文字"（标题/正文/副标），**不含 items**。

    为什么关键词判断必须排除 items：条目是"要点的载荷"，不是"这段在讲什么"。
    实测反例：夹具片第 3 段正文讲"画质三件事"，条目里写的是
    "rawvideo -> libx264 CRF17"，其中 "->" 命中了顺序词表，
    于是整段被误判成"流程轴"——画面上出现一条横轴连着四个不相干的要点。
    条目里的箭头/破折号是排版符号，不是叙事顺序。
    """
    chunks = []
    for k in ("big", "text", "sub", "kicker", "chapter"):
        v = line.get(k)
        if v:
            chunks.append(str(v))
    return "".join(chunks)


def _item_texts(line):
    out = []
    for it in line.get("items") or []:
        out.append(str(it.get("text") if isinstance(it, dict) else it))
    return out


def extract_number(text):
    """从一句里抽第一个"数字 + 单位"。抽不到返回 None。

    为什么排除年份：'2024 年' 是时间背景，不是"这个数字值得做成巨字卡"的信号；
    真要做数字卡，作者写 stats[] 更明确。
    """
    if not text:
        return None
    for m in _UNIT_RE.finditer(str(text)):
        raw = m.group(1)
        unit = m.group(2)
        if unit in ("年",) and _YEAR_RE.match(raw):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        return {"value": value, "raw": raw, "unit": unit}
    return None


def _numeric_items(items):
    """条目里有几条带数字（数据条的原料）。"""
    n = 0
    for it in items:
        txt = it.get("text") if isinstance(it, dict) else it
        if extract_number(txt) or (isinstance(it, dict) and it.get("value") is not None):
            n += 1
    return n


def _hit_any(text, words):
    for w in words:
        if w and w in text:
            return w
    return None


# ============================================================================
# 四、自动选型（打分 + 段长门 + 类型去重）
# ============================================================================


def score_types(line, idx, total, dur, chapter, prev_chapter, prev_types):
    """给 10 类场景打分。返回 [(type, score, reasons)]，按分数降序、平手按 SCENE_TYPES 顺序。

    打分原则：**字段存在性 > 关键词 > 结构位置**。字段是作者明说的意图，关键词是猜的。
    """
    text = _text_of(line)
    narrative = _narrative_of(line)      # 关键词只看叙述，不看条目（见 _narrative_of 注释）
    items = _item_texts(line)
    n_items = len(items)
    big = str(line.get("big") or "")
    kind = str(line.get("kind") or "body")
    first, last = (idx == 0), (idx == total - 1)
    rows = {}

    def add(t, s, why):
        if t not in SCENE_TYPES:
            return
        cur = rows.setdefault(t, [0.0, []])
        cur[0] += s
        if why:
            cur[1].append(why)

    # ---- 显式指定：压倒一切 ----
    explicit = str(line.get("type") or "").strip()
    if explicit in SCENE_TYPES:
        add(explicit, 10.0, "film.json 显式 type=%s" % explicit)

    # ---- 结构位置 ----
    # hero / outro 只在"没有更具体的内容形态"时才该赢：一段开场如果带 5 条要点，
    # 观众要看的是要点，不是封面。所以结构位置给 4~5 分，字段/形态给更高分。
    if kind == "hook" or first:
        add("hero", 4.6, "首段/开场")
    if kind == "outro" or last:
        add("outro", 5.0, "末段/收尾")
    # 章节卡是"纯标题卡"：章节变了 + 没有条目 + 标题短 + 段也短，才配得上整屏只有一个编号。
    # 三个条件都是实测加上的：
    #   · 有 items 的段用章节卡会把要点吃掉（夹具片 5 条要点的 body 曾被选成 chapter）；
    #   · 带数字/引语的段有更具体的构图，章节卡会把信息量砍掉；
    #   · 设计稿规定章节卡 ≤3.4s，长段做章节卡等于"整屏只有一个编号停 6 秒"，观众会走。
    if (chapter and chapter != prev_chapter and n_items == 0
            and len(big or text) <= 14 and float(dur) <= 4.0
            and not extract_number(text) and not _hit_any(text, QUOTE_MARKS)):
        add("chapter", 4.2, "章节变化且是纯标题卡")

    # ---- 字段存在性（作者明说的意图，权重最高） ----
    if isinstance(line.get("stats"), list) and line["stats"]:
        add("stat", 6.0, "有 stats[]")
    if isinstance(line.get("series"), list) and line["series"]:
        add("chart", 6.0, "有 series[]")
    if isinstance(line.get("sides"), dict) and line["sides"]:
        add("compare", 6.0, "有 sides{}")
    if isinstance(line.get("steps"), list) and line["steps"]:
        add("process", 6.0, "有 steps[]")
    if line.get("quote"):
        add("quote", 6.0, "有 quote")
    if n_items >= 3:
        add("list", 5.0, "items>=3")

    # ---- 关键词 / 内容形态（比字段弱，且互相竞争） ----
    # 关键：关键词命中的类型，必须真的能承载这段内容。比如"含对比词"但只有 2 条 items，
    # 画成左右双栏就是空栏；所以 compare/process/chart 都要求条目数够。
    # 权重排序的理由：list 是"有条目"的通用兜底，所以它只给 5.0；
    # 一旦条目本身带数字（chart 的原料）或全文在讲对比/流程，那些**更具体**的构图
    # 必须压过 list —— 否则"能画"永远赢过"该画"，所有带 items 的段都会退化成列表。
    num = extract_number(text)
    if num and n_items < 3:
        add("stat", 3.5, "文本含数字+单位（%s%s）" % (num["raw"], num["unit"]))
    if n_items >= 3 and _numeric_items(items) >= 3:
        add("chart", 5.5, "items 里 >=3 条带数字")
    if _hit_any(narrative, COMPARE_WORDS) and n_items >= 2:
        add("compare", 5.5, "含对比词且条目 >=2")
    if _hit_any(narrative, SEQUENCE_WORDS) and 3 <= n_items <= 6:
        add("process", 5.5, "含顺序词且条目 3~6")
    if _hit_any(narrative, QUOTE_MARKS):
        add("quote", 5.5, "含引语标记")
    if n_items == 0 and len(big or text) <= 16:
        add("statement", 3.5, "无条目且短句")

    # ---- 段长门：撑不住的类型直接出局（不是降分，是不许选） ----
    floor = float(MIN_DUR.get("statement", 2.6))
    keep = []
    for t, (s, why) in rows.items():
        need = float(MIN_DUR.get(t, floor))
        if float(dur) + 1e-9 < need:
            why = list(why) + ["段长 %.1fs < %.1fs，排除" % (dur, need)]
            s = s - 6.0                     # 记下来但排到最后，便于审计
        keep.append((t, s, why))

    # ---- 去重：上一段已经连着两次同类型，就把这个类型压到最低 ----
    if prev_types and len(prev_types) >= MAX_REPEAT:
        tail = prev_types[-MAX_REPEAT:]
        if len(set(tail)) == 1:
            t_prev = tail[0]
            for i, (t, s, why) in enumerate(keep):
                if t == t_prev and t not in ("chapter", "outro"):
                    keep[i] = (t, s - 5.0, list(why) + ["连续 %d 段同型，降权" % MAX_REPEAT])

    keep.sort(key=lambda r: (-r[1], SCENE_TYPES.index(r[0])))
    return keep


def choose_type(line, idx, total, dur, chapter, prev_chapter, prev_types):
    """选一个场景类型。返回 (type, reason[list[str]])。"""
    rows = score_types(line, idx, total, dur, chapter, prev_chapter, prev_types)
    if not rows:
        return "statement", ["无任何特征命中，退到金句构图"]
    t, s, why = rows[0]
    if s < 0:
        # 全被段长门排除：退到最耐短的类型
        return "statement", ["所有候选都被段长门排除，退到金句构图"]
    return t, why


def downgrade_for(type_name, dur):
    """段长门兜底：太短就按固定表降级。返回 (最终类型, 是否降级)。"""
    t = type_name if type_name in SCENE_TYPES else "statement"
    if float(dur) + 1e-9 >= float(MIN_DUR.get(t, 2.6)):
        return t, False
    seen = set()
    while t in DOWNGRADE and float(dur) + 1e-9 < float(MIN_DUR.get(t, 2.6)):
        if t in seen:                       # 防降级表写环（配置错误时不挂死）
            break
        seen.add(t)
        nxt = DOWNGRADE[t]
        if nxt == t:
            break
        t = nxt
    return t, True


# ============================================================================
# 五、片内变体（防"每屏同一个骨架"）
# ============================================================================


def variant_of(type_name, chapter, k, prev_variant=None):
    """本段的版式变体号（0/1/2），确定性。

    规则：先按 (chapter, type, 第几段同类型) 派生，再强制与上一段不同
    （相邻同类型同变体 = 最刺眼的"复制粘贴"）。
    """
    v = _hash_mod(3, "variant", chapter, type_name, k)
    if prev_variant is not None and v == int(prev_variant):
        v = (v + 1 + _hash_mod(2, "variant-step", chapter)) % 3
    return v


def decor_of(type_name, chapter, k):
    """装饰密度档位（0=0.55 / 1=0.75 / 2=1.0）。每 3 段回落到低档，形成呼吸。"""
    base = _hash_mod(3, "decor", chapter, type_name, k)
    return 0 if k % 3 == 2 else base


def phase_of(idx, chapter):
    """持续动态的相位偏移（弧度）。相邻段相位不同，防止同频"复印"。"""
    return 6.283185307179586 * _hash01("phase", chapter, idx)


# ============================================================================
# 六、缺省数据推导（作者没写 stats/series/steps/sides 时从 items/text 推）
# ============================================================================


def derive_stats(line, limit=3):
    """数字卡的数据：优先 stats[]，否则从 text/sub 抽第一个数字，再用 items 补 micro note。"""
    got = []
    for s in (line.get("stats") or []):
        if isinstance(s, dict) and s.get("value") is not None:
            got.append({"value": s.get("value"), "unit": str(s.get("unit") or ""),
                        "label": str(s.get("label") or ""), "note": str(s.get("note") or "")})
    if got:
        return got[:limit]
    hit = extract_number(_text_of(line))
    if not hit:
        return []
    notes = _item_texts(line)[:limit - 1]
    got.append({"value": hit["raw"], "unit": hit["unit"],
                "label": str(line.get("sub") or line.get("big") or ""),
                "note": notes[0] if notes else ""})
    for _i, t in enumerate(notes[1:]):
        got.append({"value": "", "unit": "", "label": t, "note": ""})
    return got[:limit]


def derive_series(line, limit=6):
    """数据条的数据：优先 series[]，否则用 items 里的"标签 + 数字"。"""
    got = []
    for s in (line.get("series") or []):
        if isinstance(s, dict) and s.get("label") is not None:
            got.append({"label": str(s.get("label")), "value": s.get("value"),
                        "unit": str(s.get("unit") or "")})
    if got:
        return got[:limit]
    for it in (line.get("items") or []):
        if isinstance(it, dict) and it.get("value") is not None:
            got.append({"label": str(it.get("text") or it.get("label") or ""),
                        "value": it.get("value"), "unit": str(it.get("unit") or "")})
            continue
        txt = str(it.get("text") if isinstance(it, dict) else it)
        hit = extract_number(txt)
        if hit:
            label = txt.replace(hit["raw"] + hit["unit"], "").strip(" ：:，,、-—")
            got.append({"label": label or txt, "value": hit["value"], "unit": hit["unit"]})
    return got[:limit]


def derive_steps(line, limit=5):
    """流程轴的数据：优先 steps[]，否则用 items 顺序当步骤。"""
    got = []
    for s in (line.get("steps") or []):
        if isinstance(s, dict) and s.get("label"):
            got.append({"label": str(s["label"]), "note": str(s.get("note") or "")})
        elif s:
            got.append({"label": str(s), "note": ""})
    if got:
        return got[:limit]
    for t in _item_texts(line)[:limit]:
        got.append({"label": t, "note": ""})
    return got


def derive_sides(line):
    """左右对比的数据：优先 sides{}，否则把 items 前后对半分。"""
    sides = line.get("sides")
    if isinstance(sides, dict) and sides:
        out = {}
        for k in ("left", "right"):
            blk = sides.get(k) if isinstance(sides.get(k), dict) else {}
            items = []
            for it in (blk.get("items") or []):
                items.append(str(it.get("text") if isinstance(it, dict) else it))
            out[k] = {"head": str(blk.get("head") or ""), "items": items[:3]}
        if out["left"]["items"] or out["right"]["items"]:
            return out
    items = _item_texts(line)
    if len(items) < 2:
        return None
    half = (len(items) + 1) // 2
    return {"left": {"head": "", "items": items[:half][:3]},
            "right": {"head": "", "items": items[half:][:3]}}


def derive_quote(line):
    """引语：优先 quote 字段，否则从 text 里剥掉引号/出处。"""
    q = line.get("quote")
    if q:
        return str(q), str(line.get("by") or "")
    txt = str(line.get("text") or line.get("big") or "")
    by = str(line.get("by") or "")
    m = re.search(r"[「『“\"](.+?)[」』”\"]", txt)
    if m:
        return m.group(1), by
    for sep in ("——", "—", "："):
        if sep in txt:
            head, _, tail = txt.partition(sep)
            if len(head) >= 6:
                return head.strip(), (by or tail.strip())
    return txt, by


# ============================================================================
# 七、分镜表（timeline 的 scenes 上就地补字段）
# ============================================================================


def plan_scenes(scenes, script=None, plan=None):
    """给 timeline 的每一段补 scene_type / scene_variant / scene_decor / scene_layout / scene_reason。

    scenes: K.build_timeline 产出的段列表（有 id/kind/start/end/dur 与剧本原字段）
    plan:   stylekit.resolve_script_style / designkit.design_to_plan 的结果
            （取 chapter_layout / layout_cycle / layout_default 与 style）
    返回 storyboard dict（可直接 json.dump 审计），并把字段就地写进 scenes。

    scene_layout 的取法（先命中先赢）：
      ① plan["chapter_layout"] 显式指定（按章名查，查不到用 layout_default）；
      ② plan["layout_cycle"]（≥2 种版式且没有显式 chapter_layout）：按章节首次
         出现的顺序循环取位，**同一章内版式固定**——防止"全片一张版式"；
      ③ 都没有：layout_default。

    **没有 chapter 的剧本**：按"每段自成一章"处理（轮换照常推进）。
    为什么不把空 chapter 当成"全片同一章"：那样 layout_cycle 在常见的不写 chapter
    的剧本上会退化成"永远 cycle[0]"，功能写了却完全不生效——正是本仓库反复踩过的
    "号称支持实则失效"。空 chapter 本来就没有"章"的身份可言，逐段轮换既满足
    "同章内稳定"（每段自成一章，平凡成立），又真的防住同骨架。
    """
    script = script or {}
    plan = plan or {}
    n = len(scenes)
    prev_types = []
    prev_chapter = None
    prev_variant = {}
    chapter_layout = plan.get("chapter_layout") if isinstance(plan.get("chapter_layout"), dict) else {}
    default_layout = plan.get("layout_default") or "left-stack"
    # ---- 片内版式轮换：design_to_plan 会派生 layout_cycle ----
    # 为什么需要：没有它时一章只能用一种版式（layout_default），8 段片子 8 屏同骨架 ——
    # 正好是用户原则 5 说的"同一视频内部大面积/长时间重复"。
    # 生效条件（两条都不满足就与过去逐字节一致，金帧/既有自检不漂）：
    #   ① plan 没有显式 chapter_layout（显式指定优先，不能被轮换覆盖）；
    #   ② layout_cycle 至少 2 种版式（只有一种等于不轮换）。
    # 同一章内必须稳定（观众视角一章是一段整体），换章才换版式：
    # 按「章节首次出现的顺序」依次取 cycle[0], cycle[1], … 循环。
    cycle = plan.get("layout_cycle")
    if isinstance(cycle, (list, tuple)):
        cycle = [str(x) for x in cycle if str(x or "").strip()]
    else:
        cycle = []
    use_cycle = (not chapter_layout) and len(cycle) >= 2
    # key -> 版式（保证同一章内版式不变；无 chapter 时每段自成一章，轮换照常推进）
    cycle_of = {}
    counter = {}
    rows = []
    for i, s in enumerate(scenes):
        chapter = str(s.get("chapter") or "")
        dur = float(s.get("dur") or 0.0)
        raw_type, why = choose_type(s, i, n, dur, chapter, prev_chapter, prev_types)
        stype, downgraded = downgrade_for(raw_type, dur)
        if downgraded:
            why = list(why) + ["段长 %.2fs < %.1fs，降级 %s -> %s"
                               % (dur, float(MIN_DUR.get(raw_type, 2.6)), raw_type, stype)]
        k = counter.get(stype, 0)
        counter[stype] = k + 1
        variant = variant_of(stype, chapter, k, prev_variant.get(stype))
        prev_variant[stype] = variant
        decor = decor_of(stype, chapter, k)
        if use_cycle:
            # 空 chapter 用段号做键：每段自成一"章"，轮换才真的推进
            # （否则最常见的不写 chapter 的剧本会永远停在 cycle[0]，功能形同虚设）。
            key = chapter if chapter else "\x00scene:%d" % i
            if key not in cycle_of:
                # len(cycle_of) = 这是第几个"不同章节"（0 起），按它取轮换位
                cycle_of[key] = cycle[len(cycle_of) % len(cycle)]
            layout = cycle_of[key]
        else:
            layout = chapter_layout.get(chapter) or default_layout
        s["scene_type"] = stype
        s["scene_variant"] = int(variant)
        s["scene_decor"] = int(decor)
        s["scene_layout"] = str(layout)
        s["scene_phase"] = round(phase_of(i, chapter), 4)
        s["scene_reason"] = list(why)
        rows.append({"id": s.get("id"), "kind": s.get("kind") or "body",
                     "type": stype, "variant": int(variant), "decor": int(decor),
                     "layout": str(layout), "dur": round(dur, 3),
                     "reason": list(why)})
        prev_types.append(stype)
        prev_chapter = chapter
    return {
        "version": 1,
        "generator": "grammar 0.9.0",
        "style": plan.get("style"),
        "legacy": bool(plan.get("legacy")),
        "scenes": rows,
        "type_counts": dict(counter),
    }


def describe(storyboard):
    """人读摘要（--stage layout 打印用）。"""
    if not storyboard:
        return "（无分镜）"
    counts = storyboard.get("type_counts") or {}
    order = [t for t in SCENE_TYPES if t in counts]
    parts = ["%s×%d" % (SCENE_LABELS.get(t, t), counts[t]) for t in order]
    return "%d 段 / %s" % (len(storyboard.get("scenes") or []), " ".join(parts))


def _cli(argv=None):
    """自测入口：python grammar.py [film.json] —— 只读，不渲染。

    用 argparse 解析（否则 `--help` 会被当成 film.json 路径，FileNotFoundError，
    与仓库里其它 17 个脚本的 CLI 约定不一致）。不传 film.json 时仍打印用法与 10 类构图，
    退出码 0（便于 CI 探活）。
    """
    import argparse
    import json

    ap = argparse.ArgumentParser(
        prog="grammar.py",
        description="场景语法：按内容推导每段的构图类型（只读，不渲染、不需要 timeline）。",
        epilog="不给 film.json 时只打印用法与 10 类构图说明。")
    ap.add_argument("film", nargs="?", default=None,
                    help="film.json 路径；给出时打印该剧本的分镜选型（时长按字数粗估）")
    a = ap.parse_args(argv)

    if not a.film:
        print("用法: python grammar.py film.json  # 打印该剧本的分镜选型（不需要 timeline）")
        print("场景类型:", " / ".join("%s(%s)" % (t, SCENE_LABELS[t]) for t in SCENE_TYPES))
        return 0

    try:
        with open(a.film, encoding="utf-8") as fp:
            script = json.load(fp)
    except OSError as e:
        ap.error("读不了 film.json：%s（%s）" % (a.film, e))
    if not isinstance(script, dict):
        ap.error("film.json 顶层必须是对象：%s" % a.film)
    lines = script.get("lines") or []
    if not isinstance(lines, list):
        ap.error("film.json 的 lines 必须是数组：%s" % a.film)
    # 没跑 TTS 时按字数粗估时长（只用于看选型，不是时间轴权威）
    scenes = []
    t = 0.0
    for ln in lines:
        d = max(2.0, min(12.0, len(str(ln.get("text") or "")) / 4.2 + 1.4))
        row = dict(ln, start=round(t, 3), end=round(t + d, 3), dur=round(d, 3))
        scenes.append(row)
        t += d
    sb = plan_scenes(scenes, script, {})
    print(describe(sb))
    for r in sb["scenes"]:
        print("  %-6s kind=%-9s -> %-9s v%d d%d %.1fs  %s"
              % (r["id"], r["kind"], r["type"], r["variant"], r["decor"],
                 r["dur"], "；".join(r["reason"][:2])))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
