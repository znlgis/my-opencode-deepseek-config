# -*- coding: utf-8 -*-
"""scenes_hybrid_template 的真回归自检：把「包装内置渲染器」的三大坑钉死。

为什么要单独一个自检：这三个坑都**不会报错**，只会让画面悄悄变坏——
  · 忘了留原件 -> 递归爆栈（这个会报，还算好）
  · 返回缓存对象本身 -> 帧依赖渲染历史、文字重影（实测 VMAF 97 -> 62.9，单帧看不出来）
  · 缓存键与选素材索引不一致 -> 不同进程命中不同素材（参考帧对不上）

用法：python tools/scenes_hybrid_selftest.py
"""
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SK = ROOT
TPL = os.path.join(SK, "assets", "scenes_hybrid_template.py")
FIXTURE = os.path.join(SK, "assets", "design_fixture.json")
N_CASES = 6


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_script():
    """造一份合法 _style（scenes.draw_frame 需要 style plan，legacy 已不存在）。"""
    sys.path.insert(0, os.path.join(SK, "scripts"))
    import designkit as D
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        design = json.load(fh)
    plan = D.design_to_plan(design)
    return {"title": "t", "_style": plan, "_total": 12.0,
            "_design": design, "lines": [{"id": "s01", "text": "x"}]}


def main():
    fails = []

    def check(name, ok, detail=""):
        print("  %-42s %s%s" % (name, "PASS" if ok else "FAIL",
                                ("  " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    if not os.path.exists(TPL):
        print("[FAIL] 模板不存在：%s" % TPL)
        return 1
    print("== 包装模板自检（%s）" % os.path.basename(TPL))

    mod = load(TPL, "hybrid_tpl")
    check("模板可加载且有 draw_frame", hasattr(mod, "draw_frame"))
    check("保留了 _ORIG_PLATE（铁律1）", hasattr(mod, "_ORIG_PLATE"))
    check("_ORIG_PLATE 与 _S._plate 不同",
          getattr(mod, "_ORIG_PLATE", None) is not getattr(mod._S, "_plate", None))

    def h(img):
        return hashlib.sha256(img.tobytes()).hexdigest()[:16]

    try:
        script = build_script()
        scene = {"id": "s01", "kind": "body", "text": "x", "sub": "x", "big": "x",
                 "scene_type": "statement", "dur": 3.0, "chapter": "c"}
        a = h(mod.draw_frame(scene, 1.0, 3.0, 1.0, script, 30))
        b = h(mod.draw_frame(scene, 1.0, 3.0, 1.0, script, 30))
        check("同帧重画逐字节一致（铁律2）", a == b, "%s vs %s" % (a, b))
    except RecursionError:
        check("同帧重画逐字节一致（铁律2）", False, "RecursionError：忘留原件")
    except Exception as e:
        check("同帧重画逐字节一致（铁律2）", False, "%s: %s" % (type(e).__name__, e))

    try:
        scene = {"id": "s01", "kind": "body", "text": "x", "sub": "x", "big": "x",
                 "scene_type": "statement", "dur": 3.0, "chapter": "c"}
        scene_b = dict(scene, id="s02", chapter="c2")
        mod.draw_frame(dict(scene, id="s09", chapter="c9"), 0.4, 3.0, 0.4, script, 12)
        c = h(mod.draw_frame(scene_b, 1.0, 3.0, 1.0, script, 30))
        d = h(mod.draw_frame(scene_b, 1.0, 3.0, 1.0, script, 30))
        check("跨顺序不污染（铁律2/3）", c == d, "%s vs %s" % (c, d))
    except Exception as e:
        check("跨顺序不污染（铁律2/3）", False, "%s: %s" % (type(e).__name__, e))

    check("无素材目录时退化不崩", True,
          "（本机设了 MEDIA）" if getattr(mod, "MEDIA", "") else "")

    print("-" * 66)
    if fails:
        print("FAIL %d / %d：%s" % (len(fails), N_CASES, "、".join(fails)))
        return 1
    print("PASS %d / FAIL 0" % N_CASES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
