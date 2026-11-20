# -*- coding: utf-8 -*-
"""intake.py — 出片前的一次性问诊：把"只有用户能决定的事"一次问完。

为什么要有这一层
----------------
提案层（propose.py）解决的是"外观由谁拍板"，但它默认**已经知道**题材、时长、气质、
声音这些输入。实际会话里这些信息常常只存在于用户的半句话里，agent 要么不问直接猜，
要么问一句挤一句 —— 结果是"有时一个不问，有时只问一两个"，用户到成片才第一次看到
自己的片子长什么样，返工全落在渲染之后。

所以把问诊固定成**一张表**：八个问题，一次问完，每个问题都带推荐项（推荐项 = 明亮、
活泼、生动的方向），用户的回答写进 work/intake.json，propose.py 读它合成 brief。

**第 2 题是"制作方式"**（自己画 / 网上素材剪辑 / 两者结合）：它决定走哪条产线——
零素材线（render.py + design.json）还是素材剪辑线（footage.py + footage.json）——
**这一步选错最贵，整片返工**。用户往往自己也说不清，所以这一题按题材给推荐：
recommend_mode() 从题材里认"要真实影像"还是"要信息图"，auto 档就用它落地并说清理由；
用户选了别的就按用户的走（MODE_ROUTES 决定下一步命令）。
脚本自己不弹窗（CLI 里没有 UI），它只负责"问什么、怎么问、答案放哪、怎么变成 brief"，
真正的提问由会话里的 question 照生成的 JSON 执行。

三个产物
--------
    work/intake.json          题目 + 答案 + 是否已确认（唯一真源）
    work/intake.brief.txt     由答案合成的 --brief 文本（可直接喂 propose.py）
    stdout                    人读的题目单 / 已填答案单

用法
----
    # 1) 生成题目（给 agent 拿去问；--json 机读）
    #    带 --topic 时「制作方式」题会附 recommended / recommend_reason（按题材算好）
    python intake.py --work work --questions
    python intake.py --work work --questions --json --topic "中国高铁十年，用真实影像"
    #    也可以先单独问推荐（不给 TEXT 就用问诊单里已写的题材）
    python intake.py --recommend "AI 大模型的三年，讲给普通观众"
    python intake.py --recommend --json
    python intake.py --work work --recommend

    # 2) 收集答案（每个 --set 一次；id:value，value 用逗号分隔多选）
    #    每个 id 的可选值以 --questions 打印出来的 options[].value 为准；
    #    duration 允许自定义原话（"不少于 12 分钟" / "45s" / "2m"，3–1800 秒）。
    python intake.py --work work --set topic:"AI 大模型的三年" --set mode:auto \
        --set tone:bright-lively --set voice:female-warm --set music:upbeat \
        --set format:1080p30 --set note:"结尾放一个二维码"

    # 3) 全部答完 -> 校验并合成 brief（制作方式 auto 按题材解析成具体产线）
    python intake.py --work work --finalize        # 写 intake.brief.txt + 打印下一步命令
    python intake.py --work work --check           # 只校验，退出码 2 = 还有没答的

    # 4) 什么都没答也要能干活（明确 --defaults 才会用推荐项补齐，绝不静默）
    python intake.py --work work --defaults
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# schema 2：新增「制作方式」题（mode）与按题材落地的产线路由（data["mode"]）。
# 旧版问诊单（schema 1）读进来不会坏，但再跑 --finalize 会因缺 mode 被判未答完——
# 这是有意的：升级后重跑问诊，就应该把新问题补问一次。
SCHEMA = 2
INTAKE_FILE = "intake.json"
BRIEF_FILE = "intake.brief.txt"

# 八个问题就是"八个决策"。顺序固定：
#   先定"给谁看什么、画面从哪来"（题材 / 制作方式 / 时长），再定"长什么样"（气质 / 画幅），
#   最后定"听着像什么"（声线 / 配乐）—— 从内容到形式，复述时用户能顺着读一遍。
#   「制作方式」紧跟在题材后面：它的推荐值由题材推出来（recommend_mode），
#   而且它决定走 render.py 还是 footage.py —— 这条走错，后面每一步都白做。
#
# 每个选项的 brief 字段：propose._brief_of 认的是自然语言关键词，
# 所以 value 直接写成它认得的中文说法（例如"深蓝""女声""硬切"），
# 不要在这里另造一套代号再翻译一次——两套映射迟早漂移。
QUESTIONS = (
    {
        "id": "topic",
        "header": "题材",
        "question": "这条片子讲什么？（一句话：题材 + 受众；例如「AI 大模型的三年，讲给普通观众」）",
        "kind": "text",
        "required": True,
        "options": [],
        "hint": "越具体越好：写清题材、讲给谁、想让人记住什么。",
    },
    {
        "id": "mode",
        "header": "制作方式",
        "question": "视频内容是自己画、网上素材剪辑，还是两者结合？",
        "kind": "single",
        "required": True,
        "default": "auto",
        # 三选一就是"画面从哪来"：自己画（零素材线）/ 网上素材剪辑（footage 线）/
        # 两者结合（按段混用）。选错产线整片返工，所以它必须是一道**必答题**；
        # auto 档由 recommend_mode 按题材落地——用户说不清时也有个带理由的默认。
        # brief 在这里只用于"认用户原话"（_norm_value 的兜底匹配），
        # 不进 intake.brief.txt：制作方式是路由，不是外观约束（见 brief_text）。
        "options": [
            {"value": "draw", "label": "自己画：代码逐帧生成（图表/信息图/文字动效/抽象动画；零素材线）",
             "brief": "自己画"},
            {"value": "footage", "label": "网上素材剪辑：找现成影像按文案剪（纪录片/新闻/实拍题材；footage 线）",
             "brief": "素材"},
            {"value": "hybrid", "label": "两者结合：素材打底 + 只叠必要信息"
                                   "（compose.py；也可按段拼接）",
             "brief": "结合"},
            {"value": "auto", "label": "你替我挑：按题材推荐（问诊单会带 recommended）", "brief": "你替我挑"},
        ],
        "hint": "自己画 = 零素材线（design.json 定外观，可提案）；网上素材剪辑 = footage 线"
                "（契约是 footage.json，提案层不适用）；两者结合有三种形态：素材打底 + 小面积叠加"
                "（compose.py，默认推荐，自绘目标 <=15%，机器审计 warn 20% / fail 30%）、按段拼接（素材段 + 图文段）、整屏卡片覆盖"
                "（基本不该用，素材会被遮掉）。别默认按段拼——实测整屏卡片会被判割裂。"
                "题材写好后加 --topic 或跑 --recommend，会给出推荐值与理由。",
    },
    {
        "id": "duration",
        "header": "时长",
        "question": "成片至少要多长？",
        "kind": "single",
        "required": True,
        "default": "at_least_3m",
        # 允许直接写时长原话（「不少于 12 分钟」/「2 分半」）：自定义档就是靠这条
        # 才可能落到具体秒数——只认预置选项的话，选了「自定义」却没有地方写数字。
        "allow_text": True,
        # 时长是**下限**（不少于），不是精确目标：成片长度永远由 TTS 实测决定，
        # 写「目标 3 分钟」而台词只够 90 秒时，渲染器只能告警、不能凭空拉长；
        # 而写「不少于 3 分钟」语义天然正确——台词不够就是没达标，要加内容。
        "options": [
            {"value": "at_least_1m", "label": "不少于 1 分钟", "brief": "不少于1分钟"},
            {"value": "at_least_3m", "label": "不少于 3 分钟", "brief": "不少于3分钟"},
            {"value": "at_least_5m", "label": "不少于 5 分钟", "brief": "不少于5分钟"},
            {"value": "at_least_7m", "label": "不少于 7 分钟", "brief": "不少于7分钟"},
            {"value": "at_least_9m", "label": "不少于 9 分钟", "brief": "不少于9分钟"},
            {"value": "custom", "label": "自定义（自己写时长，如「不少于 12 分钟」/「2 分半」）", "brief": ""},
            {"value": "auto", "label": "按台词实测长度（不设下限）", "brief": ""},
        ],
        "hint": "时长是**下限**：写成片至少要多长（例「不少于 5 分钟」）。成片实际长度仍由旁白实测决定，"
                "台词不够就是没达标——要加内容，渲染器不会截断旁白也不会拉长留白。",
    },
    {
        "id": "tone",
        "header": "主题气质",
        "question": "想要什么气质？（决定了配色明暗、活力与装饰密度）",
        "kind": "single",
        "required": True,
        "default": "bright-lively",
        # 顺序就是推荐顺序：默认项在最前，且**任何题材的推荐项都是明亮活泼**。
        "options": [
            {"value": "bright-lively", "label": "明亮活泼（推荐）：浅底、高饱和、有活力", "brief": "明亮活泼"},
            {"value": "bright-clean", "label": "明亮干净：浅底、低饱和、留白多", "brief": "明亮"},
            {"value": "bright-warm", "label": "明亮温暖：暖色浅底，亲和", "brief": "明亮，暖纸"},
            {"value": "deep-focus", "label": "深色专注：暗底、克制（发布会/技术向）", "brief": "深色"},
            {"value": "auto", "label": "你替我挑（仍会先给方案看）", "brief": ""},
        ],
        "hint": "选「你替我挑」也会先出候选方案，不会跳过看图。",
    },
    {
        "id": "format",
        "header": "画幅",
        "question": "画幅与帧率？",
        "kind": "single",
        "required": False,
        "default": "1080p30",
        "options": [
            {"value": "1080p30", "label": "1920×1080 / 30fps 横屏（推荐）", "brief": ""},
            {"value": "1080p24", "label": "1920×1080 / 24fps 横屏（电影感）", "brief": ""},
            {"value": "720p24", "label": "1280×720 / 24fps 横屏（更快、更省）", "brief": ""},
            {"value": "portrait1080p30", "label": "1080×1920 / 30fps 竖屏（手机端；experimental）", "brief": ""},
            {"value": "portrait1080p24", "label": "1080×1920 / 24fps 竖屏", "brief": ""},
            {"value": "portrait720p30", "label": "720×1280 / 30fps 竖屏（更快、更省）", "brief": ""},
        ],
        "hint": "横屏 16:9 与竖屏 9:16 都支持（竖屏 experimental，版式骨架与 compare/process/chart 已竖屏原生，只有排版节奏未按竖屏复核）；只允许同比例下采样。选完写进 film.json 的 video 块；竖屏还要在 render / propose / filmcheck 上加 --layout portrait（不加就是横屏画布，与 video 块比例不符会被拒片）。",
    },
    {
        "id": "voice",
        "header": "旁白声线",
        "question": "谁来念？（声线决定整片的语气）",
        "kind": "single",
        "required": True,
        "default": "female-bright",
        "options": [
            {"value": "female-bright", "label": "明亮女声（推荐）：轻快、有精神", "brief": "女声"},
            {"value": "male-bright", "label": "明亮男声：青年、清爽", "brief": "男声"},
            {"value": "female-warm", "label": "温暖女声：柔、慢一点", "brief": "女声"},
            {"value": "male-steady", "label": "沉稳男声：纪录片口吻", "brief": "男声"},
            {"value": "auto", "label": "你替我挑", "brief": ""},
        ],
        "hint": "具体音色由设计里的 voice 决定；这里只定方向（性别/语气）。",
    },
    {
        "id": "music",
        "header": "配乐",
        "question": "配乐要多热闹？",
        "kind": "single",
        "required": True,
        "default": "upbeat",
        "options": [
            {"value": "upbeat", "label": "轻快上扬（推荐）：BPM ~118，收束明亮", "brief": "上扬"},
            {"value": "hopeful", "label": "温暖向上：BPM ~92，叙事感", "brief": "希望"},
            {"value": "steady", "label": "稳稳推进：BPM ~104，中性", "brief": "稳"},
            {"value": "driving", "label": "强驱动：BPM ~124，节奏感强", "brief": "推进"},
            {"value": "quiet", "label": "克制留白：BPM ~78，配乐很轻", "brief": "平静"},
            {"value": "auto", "label": "你替我挑", "brief": ""},
        ],
        "hint": "配乐由 musicgen 按参数现场编排，没有现成曲风可选。",
    },
    {
        "id": "note",
        "header": "补充要求",
        "question": "还有别的硬要求吗？（可留空：必须出现/不要出现的东西，例如「不要放射线」「结尾放二维码」）",
        "kind": "text",
        "required": False,
        "options": [],
        "hint": "「不要 X」会进 avoid，「要 X」会进 must_have；认不出的词会被原样提示，不会静默丢弃。",
    },
)

_BY_ID = {q["id"]: q for q in QUESTIONS}

# ---------------------------------------------------------------- 制作方式（第 2 题）
# 这一题问的是"画面从哪来"，答案是**产线路由**，不是外观约束：
#   draw    零素材线：render.py + design.json（外观由提案冻结）
#   footage 素材剪辑线：footage.py + footage.json（提案层不适用）
#   hybrid  两条线按段拼：技能没有一键命令，别把"结合"当成"能自动出片"
# 用户往往说不清自己要哪种，所以 auto 档按题材落到具体产线；显式选择永远优先。
MODE_DRAW = "draw"
MODE_FOOTAGE = "footage"
MODE_HYBRID = "hybrid"
MODE_AUTO = "auto"

MODE_LABELS = {
    MODE_DRAW: "自己画（代码逐帧生成）",
    MODE_FOOTAGE: "网上素材剪辑",
    MODE_HYBRID: "两者结合",
    MODE_AUTO: "你替我挑",
}

# 推荐落地后"下一步走哪条线"：finalize 直接打出来，不让 agent 自己再判一次。
MODE_ROUTES = {
    MODE_DRAW: ("零素材线", "propose.py 定设计 -> render.py 出片（design.json 决定外观）"),
    MODE_FOOTAGE: ("素材剪辑线", "footage.py 一条龙（契约 footage.json；提案层不适用）"),
    # hybrid 的**默认形态**是形态 3（素材打底 + 小面积叠加）：素材就是画面，
# 只在必要处叠一行对照/一个标注。为什么不是"按段拼接"：实测整屏图文段会让
# 片子变成"两部片拼一起"，而且图文段外观还得再走一遍 propose。
# 形态 1（按段拼接）保留为**显式选择**（--hybrid-form 1），适合"数据/流程段
# 本身就独立成篇"的题材。
    MODE_HYBRID: ("素材打底 + 小面积叠加（形态 3，默认）",
                  "footage.py --stage all 出底片与混音 -> 写 overlay.json（只在必要处叠小面积信息）"
                  " -> compose.py --stage all 叠层 + 字幕 + 响度归一（自绘面积目标 <=15%；机器审计 warn 20% / fail 30%，超限会拦）"),
}

# 题材 -> 制作方式的**推荐**词表。只影响推荐，不限制选择。
# 刻意避开"宣传片 / 解说 / 短视频"这类两边都成立的说法：它们命中只会让推荐抖动。
_FOOTAGE_WORDS = ("纪录片", "纪实", "新闻", "实拍", "影像", "历史", "老照片", "旧片",
                  "真实", "人物", "访谈", "走访", "探访", "探店", "旅行", "风景",
                  "城市", "航拍", "现场", "比赛", "赛事", "档案", "素材", "资料片",
                  "剪辑", "下载", "网上找")
_DRAW_WORDS = ("科普", "原理", "数据", "图表", "统计", "财报", "报告", "复盘",
               "流程", "教程", "教学", "知识", "概念", "抽象", "算法", "架构",
               "信息图", "动画", "产品发布", "发布会", "品牌", "规划", "自己画",
               "代码生成", "逐帧")
# 否定句不计入推荐：用户在题材/补充里写"不要真实影像、纯数据科普"时，关键词还在，
# 意思却正好相反。按"关键词前面 6 个字内出现否定词"判定（只影响推荐，不影响显式选择）。
_NEG_WORDS = ("不要", "不用", "不需要", "无需", "别用", "避免", "拒绝", "没有", "不带",
              "不做", "不加入", "不放", "不想要", "不加")
# 两边命中的比分：一边压倒另一边（>=2 倍）才判单线，否则判"结合"——
# 题材里同时出现"纪录片"和"数据图表"时，真实答案就是两条线都要。
_MODE_DOMINANCE = 2


def _hit_words(text, words):
    """命中的词；被否定的不算——"不要真实影像"里的 真实/影像 不该把推荐带到 footage。"""
    out = []
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            if not any(neg in text[max(0, i - 6):i] for neg in _NEG_WORDS):
                out.append(w)
                break
            start = i + len(w)
    return out


def recommend_mode(text):
    """按题材推荐制作方式。返回 {mode, label, why, hits}；不改变任何答案。

    判据是**关键词计数**而不是模型判断：可解释（why 里列出命中的词），也不依赖网络。
    用户说不清时它就是默认值；用户选了别的就按用户的走（见 mode_decision）。
    """
    text = str(text or "").strip()
    hits = {"footage": _hit_words(text, _FOOTAGE_WORDS),
            "draw": _hit_words(text, _DRAW_WORDS)}
    f, d = len(hits["footage"]), len(hits["draw"])
    if not text:
        mode = MODE_DRAW
        why = ("题材还没写，先按零素材线兜底；写完题材再跑 --recommend 复核"
               "（要真实影像就改选素材剪辑）")
    elif f and not d:
        mode, why = MODE_FOOTAGE, "题材里有「%s」——需要真实影像，代码画不出来" % "、".join(hits["footage"])
    elif d and not f:
        mode, why = MODE_DRAW, "题材里有「%s」——数据/原理/抽象概念，代码画最稳" % "、".join(hits["draw"])
    elif f and d and f >= d * _MODE_DOMINANCE:
        mode, why = MODE_FOOTAGE, ("真实影像的词更多（「%s」），数据类只有「%s」——"
                                   "优先素材，信息层用字幕补"
                                   % ("、".join(hits["footage"]), "、".join(hits["draw"])))
    elif not f and not d:
        mode = MODE_DRAW
        why = ("没识别出必须用真实影像的词，默认零素材线（自己画）最稳；"
               "题材里有真实人物/场地/历史画面时请改选素材剪辑")
    elif d >= f * _MODE_DOMINANCE:
        mode, why = MODE_DRAW, ("信息图的词更多（「%s」），影像类只有「%s」——"
                                "优先自己画，个别实拍镜头再补素材"
                                % ("、".join(hits["draw"]), "、".join(hits["footage"])))
    else:
        mode, why = MODE_HYBRID, ("两边都占：「%s」要真实画面，「%s」要信息层——"
                                  "素材打底 + 代码叠加"
                                  % ("、".join(hits["footage"]), "、".join(hits["draw"])))
    return {"mode": mode, "label": MODE_LABELS[mode], "why": why, "hits": hits}


def mode_decision(data):
    """答案里的制作方式 + 题材 -> 落地产线（chosen / resolved / recommended / reason / route）。

    auto 档用推荐值落地；显式选择永远优先（推荐只用来提示"和题材不太搭"）。
    每次保存都重算：用户后来改了题材，auto 档会跟着变。
    """
    answers = data.get("answers") or {}
    chosen = str(answers.get("mode") or "").strip() or MODE_AUTO
    topic = "，".join(str(answers.get(k) or "").strip() for k in ("topic", "note")
                     if str(answers.get(k) or "").strip())
    rec = recommend_mode(topic)
    resolved = rec["mode"] if chosen == MODE_AUTO else chosen
    route, how = MODE_ROUTES.get(resolved, ("", ""))
    if chosen == MODE_AUTO:
        why = "按题材推荐：%s" % rec["why"]
    elif resolved == rec["mode"]:
        why = "用户明确选择（与题材推荐一致）"
    else:
        why = ("用户明确选择（题材更推荐%s：%s；以用户选择为准，但走错线的返工代价很大）"
               % (MODE_LABELS[rec["mode"]], rec["why"]))
    return {"chosen": chosen, "resolved": resolved, "recommended": rec["mode"],
            "matches_recommendation": resolved == rec["mode"],
            "reason": why, "hits": rec.get("hits") or {}, "route": route, "how": how}


def _topic_of(work, explicit=""):
    """给推荐用的题材：显式传入优先，其次问诊单里已写的 topic+note。"""
    text = str(explicit or "").strip()
    if text:
        return text
    try:
        data = _load(intake_path(work)) or {}
    except SystemExit:                                      # 旧文件坏了不该挡住问诊
        return ""
    answers = data.get("answers") or {}
    return "，".join(str(answers.get(k) or "").strip() for k in ("topic", "note")
                    if str(answers.get(k) or "").strip())


def HYBRID_FORM_LABEL(form):
    """hybrid 的两种形态的中文标签（问诊/下一步共用一份，别在两处各写一遍）。"""
    return "3 · 素材打底 + 小面积叠加（默认）" if int(form or 3) == 3 else "1 · 按段拼接（显式选择）"


def _next_steps(mode, brief, work, form=3):
    """finalize 打印的下一步：每一档都给**能直接照抄**的命令，别只说"去剪吧"。"""
    propose = ("python <SKILL_DIR>/scripts/propose.py --brief \"%s\" "
               "--script film.json --work %s" % (brief, work))
    footage = ("python <SKILL_DIR>/scripts/footage.py --footage footage.json "
               "--work %s --out out/film.mp4 --stage all" % work)
    if mode == MODE_FOOTAGE:
        return [footage,
                "素材线不走 propose.py：外观由素材的调色与遮幅决定；契约见 references/footage.md"]
    if mode == MODE_HYBRID:
        overlay = ("python <SKILL_DIR>/scripts/compose.py --footage footage.json "
                   "--overlay overlay.json --work %s --out out/film.mp4 --stage all" % work)
        if form == 1:
            return [footage,
                    "形态 1（按段拼接）的图文段：" + propose,
                    "拼之前跑 matchstyle.py 把图文段配色/画幅锚到素材上；再 ffmpeg concat"
                    "（两段拼接必然有风格断层，先看 references/hybrid-overlay.md §0 的取舍）"]
        return [footage,
                "然后写 overlay.json（素材打底 + 只在必要处叠小面积信息，自绘面积目标 <=15%，机器审计 warn 20% / fail 30%），再跑：" + overlay,
                "默认走形态 3：素材就是画面，不需要 propose / matchstyle。\n"
                "  若内容需要整段独立成篇的图文（数据/流程），重跑 intake.py --finalize --hybrid-form 1"]
    return [propose]


def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                     # noqa: BLE001
            pass


def intake_path(work):
    return os.path.join(str(work), INTAKE_FILE)


def brief_path(work):
    return os.path.join(str(work), BRIEF_FILE)


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise SystemExit("[intake] %s 不是合法 JSON：%s" % (path, e)) from e


def _atomic_write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def question_payload(topic=""):
    """给会话层用的题目包：id / question / options(label,value) / default。

    制作方式题（mode）在给了题材时额外带 recommended / recommend_reason ——
    "根据要做的视频内容推荐哪种制作方式"的机读形态：agent 把推荐说给用户听，
    用户仍可不同意（显式选择优先）。
    """
    rec = recommend_mode(topic) if str(topic or "").strip() else None
    rows = []
    for q in QUESTIONS:
        row = {"id": q["id"], "header": q.get("header") or "", "question": q["question"],
               "multi_select": False, "required": bool(q.get("required")),
               "hint": q.get("hint") or ""}
        if q.get("options"):
            row["options"] = [{"label": o["label"], "value": o["value"]} for o in q["options"]]
        if q.get("default"):
            row["default"] = q["default"]
        if q["id"] == "mode" and rec:
            row["recommended"] = rec["mode"]
            row["recommend_reason"] = rec["why"]
        rows.append(row)
    return rows


def _default_answers():
    return {q["id"]: q["default"] for q in QUESTIONS
            if q.get("default") and q["kind"] != "text"}


def _norm_value(q, raw):
    """把用户答案规整成题目的取值：认选项 value，也认选项 label 的原话。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if q["kind"] == "text":
        return text
    # 「自定义」不是一个答案，是一个**要求用户写数字**的占位选项：
    # 用户若真把 "custom" 当答案提交，必须当场说清怎么写，不能存下一个没有秒数的字符串。
    if q.get("allow_text") and text.lower() in ("custom", "自定义"):
        raise SystemExit("[intake] %s 选了「自定义」就要写出具体时长，例如："
                         "--set %s:\"不少于 12 分钟\"（也认「2 分半」「90 秒」）"
                         % (q["id"], q["id"]))
    for o in q.get("options") or ():
        if text == o["value"]:
            return o["value"]
    low = text.lower()
    for o in q.get("options") or ():
        if low == str(o["value"]).lower() or text == o["label"]:
            return o["value"]
    # 也接受用户的原话（"明亮活泼" / "女声"）——落到第一个 brief 命中该词的选项。
    for o in q.get("options") or ():
        if o.get("brief") and o["brief"] and o["brief"] in text:
            return o["value"]
    # allow_text 的题（目前只有时长）：认出时长就存原话，由 propose 解析成秒数。
    # 不这么做的话，「自定义」档只能存一个 "custom" 字符串，等于什么都没说。
    if q.get("allow_text"):
        try:
            # 同目录、延迟导入：intake 只在有人写自定义时长时才需要解析器。
            import propose as _P                            # noqa: PLC0415
            if _P.parse_duration(text) is not None:
                return text
        except Exception:                                   # noqa: BLE001 —— 解析器不可用就按非法值报错
            pass
        if text == "custom":
            raise SystemExit("[intake] 选了「自定义」就要写出具体时长，例如："
                             "--set duration:\"不少于 12 分钟\"")
    raise SystemExit("[intake] %s=%r 不是合法选项；可选：%s"
                     % (q["id"], raw, " / ".join(o["value"] for o in q.get("options") or ())))


def _apply(data, qid, raw):
    q = _BY_ID.get(qid)
    if q is None:
        raise SystemExit("[intake] 没有这个问题：%r；只有 %s"
                         % (qid, " / ".join(q["id"] for q in QUESTIONS)))
    val = _norm_value(q, raw)
    data.setdefault("answers", {})[qid] = val
    return val


def _missing(data):
    """必答题里还没答（或答成空）的项。题目上的 required 才是判据，别在别处再抄一份。"""
    answers = data.get("answers") or {}
    return [q["id"] for q in QUESTIONS
            if q.get("required") and not str(answers.get(q["id"]) or "").strip()]


def brief_text(data):
    """答案 -> 一句 --brief 文本（propose._brief_of 解析的输入）。

    规则：题材原样保留（它是 topic）；有 brief 片段的选项按"题材，片段，片段…"拼；
    补充要求原样追加（含「不要 X」时 _brief_of 会归到 avoid）。
    """
    answers = data.get("answers") or {}
    parts = []
    topic = str(answers.get("topic") or "").strip()
    if topic:
        parts.append(topic)
    for q in QUESTIONS:
        # 制作方式（mode）不进 brief：它是产线路由，不是外观约束；
        # 往 brief 里塞"素材/自己画"只会污染 propose 的 must_have 解析。
        if q["id"] in ("topic", "mode", "note"):
            continue
        val = str(answers.get(q["id"]) or "").strip()
        if not val:
            continue
        matched = False
        for o in q.get("options") or ():
            if o["value"] == val:
                matched = True
                if o.get("brief"):
                    parts.append(o["brief"])
                break
        if not matched:
            # 自定义时长原话（"不少于 12 分钟"）：原样进 brief，由 propose 解析。
            parts.append(val)
    note = str(answers.get("note") or "").strip()
    if note:
        parts.append(note)
    return "，".join(p for p in parts if p)


def save(work, data):
    data["schema"] = SCHEMA
    _atomic_write(intake_path(work), json.dumps(data, ensure_ascii=False, indent=1))
    return intake_path(work)


def _human(data, work):
    answers = data.get("answers") or {}
    print("== 问诊单（%s）" % intake_path(work))
    for q in QUESTIONS:
        val = str(answers.get(q["id"]) or "").strip()
        mark = "OK " if val else ("?? " if q.get("required") else "-- ")
        show = val or "（未答）"
        if q.get("options") and val:
            for o in q.get("options") or ():
                if o["value"] == val:
                    show = "%s [%s]" % (o["label"], val)
                    break
        print("  %s%-8s %s" % (mark, q["id"] + ":", show))
    miss = _missing(data)
    print("  -> %s" % ("还有 %d 项没答：%s" % (len(miss), " / ".join(miss))
                       if miss else "已答完，可以 --finalize 合成 brief"))
    print("  brief: %s" % (brief_text(data) or "（空：题材都没给）"))
    mode = data.get("mode") or {}
    if mode:
        answered = str(answers.get("mode") or "").strip()
        if answered:
            print("  制作方式: 选「%s」-> %s [%s]"
                  % (MODE_LABELS.get(mode.get("chosen"), mode.get("chosen")),
                     MODE_LABELS.get(mode.get("resolved"), mode.get("resolved")),
                     mode.get("resolved")))
        else:
            # 还没答：把"如果现在按题材落地会是什么"标成推荐，别让人误以为已经答过。
            print("  制作方式: （未答）-> 按现有题材推荐 %s [%s]"
                  % (MODE_LABELS.get(mode.get("recommended"), mode.get("recommended")),
                     mode.get("recommended")))
        print("            理由: %s" % (mode.get("reason") or "-"))
        print("            走法: [%s] %s" % (mode.get("route") or "-", mode.get("how") or "-"))


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="video-studio 出片前问诊（一次问完，答案落盘）")
    ap.add_argument("--work", default="work")
    ap.add_argument("--questions", action="store_true", help="打印题目（给 question 用）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（stdout）")
    ap.add_argument("--set", action="append", default=[], metavar="ID:VALUE",
                    help="记录一条答案，可重复：--set topic:\"AI 大模型\" --set tone:bright-lively")
    ap.add_argument("--defaults", action="store_true",
                    help="用推荐项补齐未答的选项题（明确调用才生效，绝不静默）")
    ap.add_argument("--finalize", action="store_true", help="校验并写 intake.brief.txt")
    ap.add_argument("--check", action="store_true", help="只校验：还没答完退 2")
    ap.add_argument("--hybrid-form", type=int, choices=(3, 1), default=None,
                    help="两者结合时的形态：3=素材打底+小面积叠加（默认）；1=按段拼接（显式选择）")
    ap.add_argument("--show", action="store_true", help="看当前答案")
    ap.add_argument("--topic", default="",
                    help="题材原话：给「制作方式」题算推荐值（配 --questions / --recommend）")
    ap.add_argument("--recommend", nargs="?", const="", default=None, metavar="TEXT",
                    help="按题材推荐制作方式（自己画 / 网上素材剪辑 / 两者结合）；"
                         "不给 TEXT 就用问诊单里已写的题材")
    a = ap.parse_args(argv)
    _saved = _load(intake_path(a.work)) or {}
    form = int(a.hybrid_form if a.hybrid_form is not None
               else (_saved.get("hybridForm") or 3))

    try:
        if a.recommend is not None:
            text = _topic_of(a.work, a.recommend or a.topic)
            rec = recommend_mode(text)
            if a.json:
                print(json.dumps({"topic": text, "recommend": rec}, ensure_ascii=False, indent=1))
            else:
                print("== 制作方式推荐（题材：%s）" % (text or "（还没写题材）"))
                print("  推荐：%s [%s]" % (rec["label"], rec["mode"]))
                print("  理由：%s" % rec["why"])
                if rec["hits"].get("footage") or rec["hits"].get("draw"):
                    print("  命中：素材剪辑 %s / 自己画 %s"
                          % ("、".join(rec["hits"].get("footage") or []) or "（无）",
                             "、".join(rec["hits"].get("draw") or []) or "（无）"))
                print("  三条线的取舍：")
                for key in (MODE_DRAW, MODE_FOOTAGE, MODE_HYBRID):
                    print("    - %-8s %s" % (key, MODE_ROUTES[key][1]))
                print("  注：这只是推荐；问诊里用户选哪个就按哪个走（选 auto 才用推荐值）。")
            return 0

        if a.questions:
            topic = _topic_of(a.work, a.topic)
            rows = question_payload(topic)
            payload = {"schema": SCHEMA, "work": os.path.abspath(a.work),
                       "topic": topic,
                       "instructions": ("把 questions 里的每一题都用 question 一次问完；"
                                        "多选题不要拆成多轮。制作方式题（mode）若带 recommended，"
                                        "把推荐值与理由说给用户听，但用户可以不同意。"
                                        "答完用 --set 写回，再 --finalize。"),
                       "questions": rows}
            if a.json:
                print(json.dumps(payload, ensure_ascii=False, indent=1))
            else:
                print("把下面每一题都用 question **一次**问完（不要分多轮）：")
                if topic:
                    print("（题材：%s）" % topic)
                for i, q in enumerate(rows, 1):
                    print("\n%d) [%s] %s" % (i, q["id"], q["question"]))
                    if q.get("hint"):
                        print("   %s" % q["hint"])
                    for o in q.get("options") or ():
                        star = "  <- 推荐" if q.get("default") == o["value"] else ""
                        print("     - %s%s" % (o["label"], star))
                    if q.get("recommended"):
                        print("     >> 按题材推荐：%s（%s）"
                              % (MODE_LABELS.get(q["recommended"], q["recommended"]),
                                 q.get("recommend_reason") or ""))
            return 0

        data = _load(intake_path(a.work)) or {"schema": SCHEMA, "work": os.path.abspath(a.work),
                                              "answers": {}, "status": "draft"}
        old_schema = int(data.get("schema") or 0)
        if (old_schema and old_schema < SCHEMA
                and not str((data.get("answers") or {}).get("mode") or "").strip()):
            # schema 1 的问诊单没有「制作方式」：不能静默补一个模式，否则等于替用户选了产线。
            # （--defaults 是显式调用，会补 mode=auto，再由题材落地——那是用户自己要的默认，
            #  不算静默；这里只说清"还差这一题"，不替用户决定。）
            print("[intake] 问诊单是旧版（schema %d）：本次新增「制作方式」题（mode），"
                  "请先把它问一次（或显式 --defaults 用 auto 补齐）再 --finalize" % old_schema)
        if a.set:
            for item in a.set:
                if ":" not in item:
                    raise SystemExit("[intake] --set 要用 ID:VALUE，收到 %r" % (item,))
                qid, raw = item.split(":", 1)
                _apply(data, qid.strip(), raw)
            print("[intake] 已记录 %d 条答案" % len(a.set))
        if a.defaults:
            got = 0
            for q in QUESTIONS:
                if q["kind"] == "text" or not q.get("default"):
                    continue
                if not str((data.get("answers") or {}).get(q["id"]) or "").strip():
                    _apply(data, q["id"], q["default"])
                    got += 1
            print("[intake] 用推荐项补齐了 %d 项（--defaults）" % got)
        miss = _missing(data)
        data["missing"] = miss
        data["brief"] = brief_text(data)
        data["mode"] = mode_decision(data)
        data["status"] = "complete" if (not miss and str(data.get("answers", {}).get("topic") or "").strip()) else "draft"
        # --check 的契约是"只校验"（docstring / SKILL 都这么写）：以前这里无条件 save，
        # 于是"只读检查"会创建并改写 intake.json，连 mode/status 一起覆盖。
        # 只有用户显式带了 --set / --defaults（本次确实要写盘）才落盘；否则 check 模式零写入。
        if not a.check or a.set or a.defaults:
            save(a.work, data)

        if a.check:
            if data["status"] != "complete":
                print("[intake] 还没答完（%s）：%s"
                      % (len(miss), " / ".join(miss) or "缺题材"), file=sys.stderr)
                return 2
            if a.json:
                print(json.dumps(data, ensure_ascii=False, indent=1))
            return 0
        if a.finalize:
            if data["status"] != "complete":
                print("[intake] 不能定稿：还没答完（%s）：%s"
                      % (len(miss), " / ".join(miss) or "缺题材"), file=sys.stderr)
                return 2
            txt = data["brief"]
            data["hybridForm"] = form
            _atomic_write(brief_path(a.work), txt)
            data["briefPath"] = brief_path(a.work)
            save(a.work, data)
            if a.json:
                print(json.dumps(data, ensure_ascii=False, indent=1))
            else:
                mode = data.get("mode") or {}
                resolved = mode.get("resolved") or MODE_DRAW
                route, _how = MODE_ROUTES.get(resolved, ("", ""))
                print("== 已定稿")
                print("  brief: %s" % txt)
                print("  制作方式: %s [%s]（用户选「%s」，题材推荐「%s」）%s"
                      % (MODE_LABELS.get(resolved, resolved), resolved,
                         MODE_LABELS.get(mode.get("chosen"), mode.get("chosen")),
                         MODE_LABELS.get(mode.get("recommended"), mode.get("recommended")),
                         "" if mode.get("matches_recommendation") else "  <- 与推荐不一致"))
                print("  理由: %s" % (mode.get("reason") or "-"))
                print("  下一步（%s）: %s" % (route, mode.get("how") or "-"))
                if resolved == MODE_HYBRID:
                    print("  形态: %s" % (HYBRID_FORM_LABEL(3) if form == 3
                                            else HYBRID_FORM_LABEL(1)))
                for ln in _next_steps(resolved, txt, a.work, form=form):
                    print("    " + ln)
            return 0
        if a.json:
            print(json.dumps(data, ensure_ascii=False, indent=1))
        elif a.show or a.set or a.defaults:
            _human(data, a.work)
        else:
            _human(data, a.work)
        return 0
    except SystemExit:
        raise
    except Exception as e:                                    # noqa: BLE001
        raise SystemExit("[intake] %s: %s" % (type(e).__name__, e)) from e


if __name__ == "__main__":
    sys.exit(main())
