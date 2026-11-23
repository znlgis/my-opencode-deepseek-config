# -*- coding: utf-8 -*-
"""qatext.py — 版式文字冲突门禁（G13）。

为什么需要它
------------
filmkit.qa_text_safe 只量**字幕**的宽高/行数/触底，**不看任意两个文本框是否相交**；
PIL 路线的十种构图各自摆绝对坐标（kicker/title/sub/panel），一旦两个元素算到同一块位置，
成片里就是"字压字"，而容器层、信号层、时间层门禁全都查不出来（实测整片压字、
12 道门禁全绿）。web 引擎有 overlaps 检测（webengine.py 的 textqa），PIL 路线没有——
**同一个产品里的两条渲染路线，质检能力不对等**。本模块把这条补齐。

判据
----
采集：scenes.py 的 _draw_text() 是所有文字的唯一出口，text_qa_start() 打开后
每画一行就记一条最终落笔矩形（align/tracking/视差/入场偏移都已算进去）。

判定（按场景，取段内若干关键帧）：
  * 两个不同文本的框**相交面积** >= 较小框面积的 18% -> error；
  * 框越出 SAFE 安全区（上下左右各 2px 容差）-> error；
  * 框落进字幕带 SUB_BAND -> warning（字幕带由 frame 层叠在画面上，正文不该压进去）；
  * 角色为 hud / number / decor 的框不参与两两相交判定（设计允许它们与正文重叠）。

三档语义（与其它门禁一致）：
  * pass：真的查了，且 0 error；
  * fail：查了，有 error（阻断）；
  * skipped：没有 scenes.py / 场景不支持采集 / 本段没有文字 —— **写明原因，不算通过**。
"""
from __future__ import annotations

MIN_SIDE = 8.0          # 小于这个边长的框当"点装饰"，不参与相交（如单字标点）
OVERLAP_FRAC = 0.18     # 相交面积 >= 较小框的 18% 判冲突
SAFE_TOL = 2.0          # 安全区容差（像素）
ROLE_EXEMPT = ("hud", "number", "decor")


def _area(b):
    """box 是 (text, x0, y0, x1, y1, size, role)，面积取 1..4 四个坐标。"""
    return max(0.0, b[3] - b[1]) * max(0.0, b[4] - b[2])


def _inter(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def find_conflicts(boxes, safe=None, sub_band=None):
    """纯函数：给一组 (text,x0,y0,x1,y1,size,role)，返回 (errors, warnings)。"""
    errs, warns = [], []
    rows = [b for b in boxes if _area(b) > 0]
    for b in rows:
        if b[6] in ROLE_EXEMPT:
            continue
        x0, y0, x1, y1 = b[1], b[2], b[3], b[4]  # noqa: E501 (box: text,x0,y0,x1,y1,size,role)
        if safe:
            if (x0 < safe[0] - SAFE_TOL or x1 > safe[2] + SAFE_TOL
                    or y0 < safe[1] - SAFE_TOL or y1 > safe[3] + SAFE_TOL):
                errs.append("文字越出安全区：%r [%.0f,%.0f]-[%.0f,%.0f]（SAFE %s）"
                            % (str(b[0])[:18], x0, y0, x1, y1, tuple(int(v) for v in safe)))
        if sub_band and _inter((x0, y0, x1, y1), (sub_band[0], sub_band[1],
                                                  sub_band[2], sub_band[3])) > 0:
            warns.append("正文压进字幕带：%r y=%.0f..%.0f（字幕带 %s）"
                         % (str(b[0])[:18], y0, y1, tuple(int(v) for v in sub_band)))
    body = [b for b in rows if b[6] not in ROLE_EXEMPT]
    for i in range(len(body)):
        for j in range(i + 1, len(body)):
            a, b = body[i], body[j]
            if str(a[0]).strip() == str(b[0]).strip():
                continue                    # 同一个字不跟自己报
            if min(a[3] - a[1], a[4] - a[2]) < MIN_SIDE or \
               min(b[3] - b[1], b[4] - b[2]) < MIN_SIDE:
                continue
            ov = _inter((a[1], a[2], a[3], a[4]), (b[1], b[2], b[3], b[4]))
            if ov <= 0:
                continue
            frac = ov / max(1.0, min(_area(a), _area(b)))
            if frac >= OVERLAP_FRAC:
                errs.append("文字压盖 %.0f%%：%r 与 %r（框 %s / %s）"
                            % (frac * 100, str(a[0])[:16], str(b[0])[:16],
                               [int(x) for x in a[1:5]], [int(x) for x in b[1:5]]))
    return errs, warns


def _scene_frames(scene, fracs=(0.35, 0.55, 0.8)):
    dur = float(scene.get("dur") or 0.0)
    return [max(0.0, min(dur - 1e-3, dur * float(f))) for f in fracs]


def scan(tl, scenes_mod, script, safe=None, sub_band=None, max_scenes=None):
    """逐场景重渲关键帧并收集文字框，返回报告 dict。

    约束：必须**进程内**调用 scenes_mod（采集器是模块级全局），不能起子进程。
    """
    rep = {"errors": [], "warnings": [], "checked": 0, "skipped": [],
           "boxes": 0}
    if scenes_mod is None or not hasattr(scenes_mod, "text_qa_start"):
        rep["skipped"].append("scenes.py 不支持文字框采集（旧版/自写渲染器）：本片没查")
        return rep
    scenes = list((tl or {}).get("scenes") or [])
    if max_scenes:
        scenes = scenes[:int(max_scenes)]
    for s in scenes:
        stype = str(s.get("scene_type") or s.get("type") or "")
        any_txt = bool(s.get("big") or s.get("text") or s.get("kicker")
                       or s.get("items") or s.get("stats") or s.get("series")
                       or s.get("steps") or s.get("sides") or s.get("quote"))
        if not any_txt:
            rep["skipped"].append("%s: 本段无文字" % s.get("id"))
            continue
        got = 0
        failed = False
        for lt in _scene_frames(s):
            scenes_mod.text_qa_start()
            try:
                scenes_mod.draw_frame(s, lt, float(s.get("dur") or 0.0),
                                      float(s.get("start") or 0.0) + lt, script,
                                      int(round((float(s.get("start") or 0.0) + lt)
                                                * float(tl.get("fps") or 30))))
            except Exception as e:                              # noqa: BLE001
                rep["skipped"].append("%s: 采集帧渲染失败 %s" % (s.get("id"), e))
                scenes_mod.text_qa_take()
                failed = True
                break
            boxes = scenes_mod.text_qa_take()
            got += len(boxes)
            errs, warns = find_conflicts(boxes, safe=safe, sub_band=sub_band)
            for e in errs:
                rep["errors"].append("%s@%.2fs %s" % (s.get("id"), lt, e))
            for w in warns:
                rep["warnings"].append("%s@%.2fs %s" % (s.get("id"), lt, w))
        if failed:
            continue
        rep["checked"] += 1
        rep["boxes"] += got
        if got == 0:
            rep["skipped"].append("%s(%s): 采集到 0 个文字框" % (s.get("id"), stype))
    return rep


def gate_result(rep, name="G13"):
    """把 scan() 的结果转成账本行（与 render.py 的 GateLedger 对齐）。

    三档语义必须严格（顺序也有讲究）：
      * 有 error 就是 fail——**哪怕同时有段没查完**：错就是错，不能被 skipped 吞掉
        （旧实现先判"checked==0 且全跳过"，于是"首帧查到压盖、次帧渲染崩"会被记成
        skipped，压盖证据直接消失）；
      * 一段都没真正检查（checked==0）记 skipped，不能因为"没报错"就写成 pass；
      * 有文字的场景**一个文字框都没采到**（boxes==0）说明采集钩子失效/全被裁掉，
        这不是"0 处压盖"，只能记 skipped。
    """
    checked = int(rep.get("checked") or 0)
    boxes = int(rep.get("boxes") or 0)
    why = [str(x) for x in (rep.get("skipped") or [])]
    if rep.get("errors"):
        return dict(gate=name, status="fail",
                    detail="%d 处版式冲突：%s" % (len(rep["errors"]),
                                                 "；".join(rep["errors"][:3])),
                    values={"errors": rep["errors"][:20],
                            "scenes": checked, "boxes": boxes})
    if checked == 0:
        return dict(gate=name, status="skipped",
                    detail="没有可检查的场景（%s）"
                           % ("；".join(why[:2]) or "时间轴里没有段"))
    if boxes == 0:
        return dict(gate=name, status="skipped",
                    detail="%d 段有文字但一个文字框都没采到（采集钩子失效或框全被裁掉，"
                           "不能当作 0 处压盖）：%s"
                           % (checked, "；".join(why[:2]) or "采集端没记录任何落笔矩形"),
                    values={"scenes": checked, "boxes": 0})
    if rep.get("skipped"):
        return dict(gate=name, status="pass",
                    detail="%d 段 / %d 个文字框，0 处压盖（%d 段跳过：%s）"
                           % (rep["checked"], rep["boxes"], len(rep["skipped"]),
                              rep["skipped"][0]),
                    values={"scenes": rep["checked"], "boxes": rep["boxes"]})
    return dict(gate=name, status="pass",
                detail="%d 段 / %d 个文字框，0 处压盖" % (rep["checked"], rep["boxes"]),
                values={"scenes": rep["checked"], "boxes": rep["boxes"]})
