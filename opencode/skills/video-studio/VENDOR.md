# Vendored from `dsh-video-studio-skill`

本技能从上游仓库收编，脚本主体保持原样；只有与 OpenCode 集成、以及 `schemas/` `tools/` 的目录位置有关的改动。

| 项 | 值 |
| --- | --- |
| 上游 | <https://github.com/znlgis/dsh-video-studio-skill> |
| 版本 | 2.7.1（`package.json` / `CHANGELOG.md`） |
| 收编日期 | 2026-11-20（上一版 2.0.0，2026-11-18） |
| 上游许可 | MIT（见 `LICENSE`；三方致谢见 `THIRD_PARTY_NOTICES.md`） |

## 收编了什么

- `skills/video-studio/` 全量：`SKILL.md`、`references/`（17 篇）、`scripts/`（35 个）、`assets/`、`requirements.txt`
- 上游包根的 `schemas/*.json` → 本技能 `schemas/`（`design` / `film` / `footage` / `proposal` 四份）
- 上游包根的运行期工具 → 本技能 `tools/`：`estimate.py`、`golden_frames.py`、`silhouette_qa.py`、
  `audio_similarity.py`、`footage_selftest.py`、`matchstyle_selftest.py`、`scenes_hybrid_selftest.py`

## 没搬什么

- 插件壳：`index.js`、`cordis.patch.yml`、`locale/`、`icon.svg`、`package.json`（OpenCode 没有对应机制）
- 仓库 / CI 专用：`tools/ci_check.py`、`tools/selftest_noscipy.py`、`tools/plugin_entry_check.mjs`、`.github/`

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
     这些**漏改会静默 skip**（金帧/剪影两条门禁变成"永远没查"），所以改完必须看 selfcheck 是不是 97 PASS、SKIP 0。
   - `scripts/selfcheck.py` 第 22 条（web 页面契约）的回显用 `os.path.relpath(web, os.getcwd())`；技能装在 `C:`、
     工作目录在 `D:` 时抛 `ValueError: path is on mount 'C:', start on mount 'D:'`，整条检查判 FAIL。
     已加退化分支，跨盘符改用绝对路径（上游 2.0.0 修过、2.7.1 又回退了；这条只在跨盘符下暴露）。
2. **提问工具**：`ask_user_question` → OpenCode `question`（`SKILL.md`、`scripts/intake.py`、`references/`）。
3. **看图工具**：`read_image` → `read`（OpenCode 的 `read` 直接返回图片）。
4. **设计台账默认路径**：`~/.dsh/video-studio/design-ledger.json` → `~/.config/opencode/video-studio/design-ledger.json`
   （`scripts/propose.py`、`SKILL.md`、`references/proposal.md`；`VIDEO_STUDIO_LEDGER` 环境变量与 `--no-ledger` / `--show-ledger` 行为不变）。
5. **路径占位符**：`<REPO>` 全部改成 `<SKILL_DIR>`；`<SKILL_DIR>` 的定义改成 OpenCode 的全局技能目录
   （`$env:USERPROFILE\.config\opencode\skills\video-studio`），并把「`tools/`/`schemas/` 在 `<REPO>` 下」那句改写成
   「两者都在 `<SKILL_DIR>` 下」。`references/audio-variation.md` 里按仓库根写死 `skills/video-studio/` 前缀的命令同步改成 `<SKILL_DIR>`。
6. **`ci_check` 引用**：删掉「仓库级体检」一节与散落的 `tools/ci_check.py` 提法，改成 selfcheck 是唯一可用性判据
   （`SKILL.md`、`references/audio-variation.md`、`references/quality-gates.md`、`references/scene-grammar.md`、
   `references/script-index.md`、`requirements.txt`、`scripts/selfcheck.py` 的注释）。
7. **文档里的总数**：`references/audio-variation.md`、`references/quality-gates.md`、`references/scene-grammar.md`、
   `scripts/studio.py` 里写死的旧条数（77 / 85）统一改成真实的 97（上游自己漂了，不是收编引入的）。

## 重新 vendor

从上游目标版本复制，然后重放上面的 1–7 条；改完必须跑：

```powershell
$S = "$env:USERPROFILE\.config\opencode\skills\video-studio"   # 仓库内改完先跑 scripts\sync-config.ps1
# 跨盘符那条只有"技能在 C:、cwd 在 D:"时才暴露，本仓库的常规工作目录在 D:，所以这条务必就在 D: 下跑
cd D:\self\code\my-opencode-deepseek-config
python "$S\scripts\selfcheck.py"                    # PASS 97 / FAIL 0 / SKIP 0
python "$S\tools\footage_selftest.py"               # 7 / 7（素材线 F1–F8，离线约 30 秒）
python "$S\tools\matchstyle_selftest.py"            # 5 / 5（hybrid 风格对齐 + 黑边回填）
python "$S\tools\scenes_hybrid_selftest.py"         # 6 / 6（形态 3 模板帧纯度）
python "$S\scripts\filmcheck.py" "$S\assets\film.json"   # 0 error / 0 warning
```

本仓库收编 2.7.1 时实测：Windows 11、Python 3.12.16、ffmpeg 9.0.2 full build、numpy 2.5.4、Pillow 13.0.0，
`selfcheck` **PASS 97 / FAIL 0 / SKIP 0**（含横竖两个 profile 的金帧像素比对、97 条里的 51 号）。
`requirements.txt` 的 Pillow 上界是 `<14`，13.x 已在实测范围内。
