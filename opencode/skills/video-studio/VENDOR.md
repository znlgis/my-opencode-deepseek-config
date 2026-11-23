# Vendored from `dsh-video-studio-skill`

本技能从上游仓库收编，脚本主体保持原样；只有与 OpenCode 集成、以及 `schemas/` `tools/` 的目录位置有关的改动。

| 项 | 值 |
| --- | --- |
| 上游 | <https://github.com/znlgis/dsh-video-studio-skill> |
| 版本 | 2.7.3（`package.json` / `CHANGELOG.md`，commit `adc97f8`） |
| 收编日期 | 2026-11-23（上一版 2.7.1，2026-11-20） |
| 上游许可 | MIT（见 `LICENSE`；三方致谢见 `THIRD_PARTY_NOTICES.md`） |

## 收编了什么

- `skills/video-studio/` 全量：`SKILL.md`、`references/`（20 篇）、`scripts/`（31 个，2.7.3 新增 `qatext.py`）、`assets/`、`requirements.txt`
- 上游包根的 `schemas/*.json` → 本技能 `schemas/`（`design` / `film` / `footage` / `proposal` 四份）
- 上游包根的运行期工具 → 本技能 `tools/`：`estimate.py`、`golden_frames.py`、`silhouette_qa.py`、
  `audio_similarity.py`、`footage_selftest.py`、`matchstyle_selftest.py`、`scenes_hybrid_selftest.py`

2.7.3 相对 2.7.1 的主要变化（重放补丁时按这些判断哪些是上游行为）：

- 新增 `scripts/qatext.py`：版式文字冲突门禁 `G13`（相交 / 越出 SAFE / 压字幕带），采集端在 `assets/scenes.py`
  （`text_qa_start/take`）。`scenes.py` 没有钩子时 G13 记 `skipped`，报告必须写明。
- 素材线门禁从 F1–F8 扩到 **F1–F9**（F9 字幕排版：行数 / 像素宽 / 不越底）。
- 四份 schema 同步扩展；`selfcheck` 97 → **105 条**；`studio.py` 改为现场读 `selfcheck.CASES`，不再写死条数。
- 时间轴 / ASR 偏移 / 混音真峰 / 局部 stage 账本 / 并发原子写等约 90 处修复。

## 没搬什么

- 插件壳：`index.js`、`cordis.patch.yml`、`locale/`、`icon.svg`、`package.json`（OpenCode 没有对应机制）
- 仓库 / CI 专用：`tools/ci_check.py`、`tools/selftest_noscipy.py`、`tools/plugin_entry_check.mjs`、`.github/`
- 只被上面那个 `ci_check.py` 使用的回归夹具：`assets/footage/regression/`（3 个文件约 190 KB，本收编版没有任何代码读它）

## 与上游的差异（重新 vendor 时按这份清单重放）

1. **目录布局**：上游是「包根 `tools/` + `schemas/`，技能在 `<包根>/skills/video-studio/`」，本仓库把两者
   **挪进了技能目录**。受影响的路径计算：
   - `tools/estimate.py` ×3：`Path(__file__).resolve().parent.parent / "skills" / "video-studio" / "scripts"`
     → `.parent.parent / "scripts"`（`parent.parent` 已经是技能根）
   - `tools/audio_similarity.py`：`HERE.parent / "skills" / "video-studio" / "scripts"` → `HERE.parent / "scripts"`
   - `tools/golden_frames.py`、`tools/silhouette_qa.py`：`SKILL = os.path.join(ROOT, "skills", "video-studio")`
     → `SKILL = ROOT`（`ROOT = dirname(HERE)` 即技能根）
   - `tools/footage_selftest.py`、`tools/matchstyle_selftest.py`：`SCRIPTS = ROOT / "skills" / "video-studio" / "scripts"`
     → `ROOT / "scripts"`；`matchstyle_selftest.py` 里的 `ROOT / "skills" / "video-studio" / "assets"` 同改为 `ROOT / "assets"`
   - `tools/scenes_hybrid_selftest.py`：`SK = os.path.join(ROOT, "skills", "video-studio")` → `SK = ROOT`
   - `scripts/qaasr.py`：`os.path.join(HERE, "..", "..", "assets", "models", name)` → `os.path.join(HERE, "..", "assets", "models", name)`
     （上游注释写的 `<repo>/skills/video-studio/assets/models`，实际解析到了 `skills/assets`；挪进技能目录后正好对齐）
   - `scripts/selfcheck.py`：4 处 `root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))` → `os.path.dirname(HERE)`；
     2 处 `os.path.join(root, "skills", "video-studio", "assets", …)` → `os.path.join(root, "assets", …)`；
     1 处 `skill = os.path.join(root, "skills", "video-studio")` → `skill = root`。
     2.7.3 新增的第 102 条还写死了 `os.path.join(here, "..", "..", "..", "schemas", "design.schema.json")`，
     要改成 `os.path.join(here, "..", "schemas", "design.schema.json")`；改漏会直接 **FAIL 102（FileNotFoundError）**。
   - `scripts/selfcheck.py` 第 54 条（场景类型单真值）：schema 路径原来写
     `os.path.join(dirname(dirname(here_scripts)), "..", "..", "schemas", …)`——从 `<包根>/skills` 再退两级
     就出了包根，**上游这里本来就多退了一级**，第 54 条一直是"标题写 4 处、实际只比 3 处"。收编版改成
     `os.path.join(os.path.dirname(here_scripts), "schemas", "film.schema.json")`，现在真的是 4 处。
   - `scripts/selfcheck.py` 第 22 条（web 页面契约）的回显用 `os.path.relpath(web, os.getcwd())`；技能装在 `C:`、
     工作目录在 `D:` 时抛 `ValueError: path is on mount 'C:', start on mount 'D:'`，整条检查判 FAIL。
     已加退化分支，跨盘符改用绝对路径（上游 2.0.0 修过、2.7.1 与 2.7.3 都回退了；这条只在跨盘符下暴露）。
   - 以上 selfcheck 的几处**漏改会静默 skip 或静默降级**（金帧/剪影/单真值门禁变成"永远没查"），
     所以改完不能只看 exit code，必须确认 `PASS 105 / FAIL 0 / SKIP 0`，并抽查第 51/54/55/76/102 条的详情行。
2. **提问工具**：`ask_user_question` → OpenCode `question`（`SKILL.md`、`scripts/intake.py`）。
3. **看图工具**：`read_image` → `read`（OpenCode 的 `read` 直接返回图片；命中 `SKILL.md` 与 `references/frame-qa.md`、
   `pipeline.md`、`quality-gates.md`）。
4. **设计台账默认路径**：`~/.dsh/video-studio/design-ledger.json` → `~/.config/opencode/video-studio/design-ledger.json`
   （`scripts/propose.py`、`SKILL.md`、`references/proposal.md`；`VIDEO_STUDIO_LEDGER` 环境变量与 `--no-ledger` / `--show-ledger` 行为不变）。
5. **路径占位符**：`<REPO>` 全部改成 `<SKILL_DIR>`；`<SKILL_DIR>` 的定义改成 OpenCode 的全局技能目录
   （`$env:USERPROFILE\.config\opencode\skills\video-studio`），并把「`tools/`/`schemas/` 在 `<REPO>` 下」那句改写成
   「两者都在 `<SKILL_DIR>` 下」。
   - **不要无脑全文替换**：`references/script-index.md` 里原本是「`<SKILL_DIR>` = resourceBase；`<REPO>` = 包根」，
     替换后会变成「`<SKILL_DIR>` = …；`<SKILL_DIR>` = 包根」这种自相矛盾的句子，必须按语义重写。
   - 散文/代码注释里的 `skills/video-studio/scripts/x.py:12` 这类**行号引用保留不动**：它仍是真实路径的后缀，
     且重写一遍只会在下次 vendor 时制造无意义 diff。唯一例外是 2.7.3 里已经漂移的
     `references/scene-grammar.md` 第 54 条行号（`selfcheck.py:1526`），已改成函数名
     `t_scene_types_single_truth`，避免下次再漂。
6. **`ci_check` 引用**：删掉「仓库级体检」一节与散落的 `tools/ci_check.py` 提法，改成 selfcheck 是唯一可用性判据
   （`SKILL.md` §10/§13、`references/audio-variation.md`、`references/quality-gates.md`、`references/scene-grammar.md`、
   `references/script-index.md`、`requirements.txt`、`scripts/selfcheck.py` 的注释）。
7. **文档里的总数与门禁编号**：2.7.3 的上游文档已把 selfcheck 更新到 105 条、脚本 31 个，但仍有三处漂移，收编时一并修：
   - `references/script-index.md` 的 selfcheck 行只列到第 97 条 → 补 98–105（G13 纯函数 / 场景文字采集 /
     章节卡与要点列表不重叠 / 局部 stage 保留交付账本 / schema required 对齐 / voice mode=wav / persona 移除 / SAFE 钳回）。
   - `references/script-index.md` 没有 `qatext.py` 行 → 补一行（G13 的实现与 `skipped` 条件）。
   - `references/scene-grammar.md` 把剪影门禁写成「G13 的覆盖范围」→ 改为 `S13`（2.7.3 起账本 G13 是版式文字冲突，
     文档已在别处统一为 S13）。
8. **文档里的可执行命令**：
   - `SKILL.md` §1 的 `propose.py` 示例缺续行符（`--brief "…"` 行末没有 `\`），照抄会变成两条命令、
     第一条缺 `--script/--work`；已补 `\`。
   - `references/footage.md` 的 `python tools/footage_selftest.py` 改成 `<SKILL_DIR>/tools/…`。
   - `references/audio.md`、`SKILL.md` 里 `tools/` 命令后面的「（在包根执行）」全部删掉——收编后没有"包根"。
9. **`studio.py` 的脚本计数**：2.7.3 已改为现场读 `selfcheck.CASES`，不再需要手改；但文件头仍写「31 个脚本」，
   与当前 `scripts/` 实际数量一致，保留即可。

## 重新 vendor

从上游目标版本复制，然后重放上面的 1–9 条；改完必须跑：

```powershell
$S = "$env:USERPROFILE\.config\opencode\skills\video-studio"   # 仓库内改完先跑 scripts\sync-config.ps1
# 跨盘符那条只有"技能在 C:、cwd 在 D:"时才暴露，本仓库的常规工作目录在 D:，所以这条务必就在 D: 下跑
cd D:\self\code\my-opencode-deepseek-config
python "$S\scripts\selfcheck.py"                    # PASS 105 / FAIL 0 / SKIP 0
python "$S\tools\footage_selftest.py"               # 7 / 7（素材线 F1–F9，离线约 30 秒）
python "$S\tools\matchstyle_selftest.py"            # 5 / 5（hybrid 风格对齐 + 黑边回填）
python "$S\tools\scenes_hybrid_selftest.py"         # 6 / 6（形态 3 模板帧纯度）
python "$S\scripts\filmcheck.py" "$S\assets\film.json"   # 0 error / 0 warning
```

**补丁方式**：逐行精确替换（改哪行动哪行），**不要整篇重写 Python 文件**——某些写入路径（包括直接调用
`edit` 工具的编辑器集成）会触发格式化器把全文重排，几百行无意义 diff 会把下次 vendor 的真正改动埋掉
（本项目踩过两次：`qaasr.py` / `selfcheck.py` 一度被整体重排，语义 diff 从 20 行变成 2000+ 行）。
Python 补丁建议用 `str.replace` 脚本落盘（`newline=""`、UTF-8 无 BOM），改完用
`git diff --no-index --numstat --ignore-cr-at-eol <上游文件> <收编文件>` 核对：每个文件的差值应该等于
上面清单里点名的行数。

本仓库收编 2.7.3 时实测：Windows 11、Python 3.12.16、ffmpeg 9.1 full build、numpy 2.5.4、Pillow 13.0.0，
`selfcheck` **PASS 105 / FAIL 0 / SKIP 0**（含横竖两个 profile 的金帧像素比对、第 51/54/55/76/102 条）。
`requirements.txt` 的 Pillow 上界是 `<14`，13.x 已在实测范围内。
