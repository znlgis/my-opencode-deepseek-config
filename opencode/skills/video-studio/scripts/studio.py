# -*- coding: utf-8 -*-
"""studio.py — 薄门面：一个入口转发到现成的脚本。

为什么要有它
------------
技能里现在有 20+ 个脚本、约 200 个命令行开关。对人是记忆负担，对 agent 是**犯错概率**：
参数记不住就会瞎猜，而整套设计恰恰依赖“照文档走”。

它**不做任何事**，只做两件：
  1) 把常用动作映射到对应的脚本与参数（studio render --stage stills → render.py）；
  2) 自己不认识的参数**原样透传**给目标脚本（studio render --vmaf off 照样生效）。

刻意不动的东西：现成的 20+ 个脚本**一个都不改**，它们的 --help 与文档里的直调方式全部继续有效。
门面只是多一条路，不是替代——所以它坏了也不影响出片（这比“功能多”重要）。

用法
----
    python studio.py list
    python studio.py intake --questions
    python studio.py propose --brief <一句话> --script film.json --work work
    python studio.py render --stage stills
    python studio.py critic --work work
    python studio.py check

    python studio.py render --stage frames --jobs 8    # 未知参数原样透传

退出码：等于目标脚本的退出码（门面不该掩盖上游的失败）。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 子命令 -> (目标脚本, 一句话说明)
# 只收“高频且名字与脚本对得上”的几个；冷门脚本直调更清楚，硬塞进来等于多一层要记的映射。
COMMANDS = {
    "intake":   ("intake.py",    "出片前问诊：7 个问题一次问完"),
    "propose":  ("propose.py",   "出候选方案 / 冻结设计 / 换方向重出"),
    "render":   ("render.py",    "生产线：14 个 stage、批量、预览、断点续跑"),
    "critic":   ("critic.py",    "审美质检：离线打分 + 可选 VLM 复评"),
    "check":    ("selfcheck.py", "回归自检：76 条断言，不联网"),
    "lint":     ("designkit.py", "设计 DSL 体检（--lint design.json）"),
    "contract": ("filmcheck.py", "film.json 契约校验"),
    "explain":  ("render.py",    "哪些阶段过时了 + 最小重跑命令"),
}


def _setup_stdio():
    """Windows 管道默认 gbk，会把中文打成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                      # noqa: BLE001
            pass


def list_commands():
    """打印子命令表。"""
    print("studio.py — 薄门面：转发到技能里的现成脚本")
    print("-" * 66)
    for name, (script, desc) in sorted(COMMANDS.items()):
        print("  %-9s -> %-13s %s" % (name, script, desc))
    print("-" * 66)
    print("未知参数原样透传；每个脚本的 --help 仍是最完整的说明书。")
    return 0


def run(name, argv):
    """把 argv 交给子命令对应的脚本，返回它的退出码。"""
    script, _desc = COMMANDS[name]
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        sys.stderr.write("缺目标脚本：%s（包不完整？）\n" % path)
        return 2
    # explain 是 render.py 的一个 stage，不是独立脚本：补上 --stage explain
    if name == "explain" and "--stage" not in argv:
        argv = ["--stage", "explain"] + list(argv)
    cmd = [sys.executable, path] + list(argv)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _setup_stdio()
    if not argv or argv[0] in ("-h", "--help", "list"):
        return list_commands()
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        sys.stderr.write("未知子命令 %r\n\n" % name)
        list_commands()
        return 2
    return run(name, rest)


if __name__ == "__main__":
    sys.exit(main())