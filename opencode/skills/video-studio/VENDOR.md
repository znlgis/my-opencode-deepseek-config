# Vendored from `dsh-video-studio-skill`

本技能从上游仓库收编，脚本主体保持原样；只有与 OpenCode 集成、以及 `schemas/` `tools/` 的目录位置有关的改动。

| 项 | 值 |
| --- | --- |
| 上游 | <https://github.com/znlgis/dsh-video-studio-skill> |
| 版本 | 2.0.0（`package.json` / `CHANGELOG.md`） |
| 收编日期 | 2026-11-18 |
| 上游许可 | MIT（见 `LICENSE`；三方致谢见 `THIRD_PARTY_NOTICES.md`） |

## 收编了什么

- `skills/video-studio/` 全量：`SKILL.md`、`references/`、`scripts/`、`assets/`、`requirements.txt`
- 上游包根的 `schemas/*.json` → 本技能 `schemas/`
- 上游包根的 `tools/{estimate,golden_frames,silhouette_qa,audio_similarity}.py` → 本技能 `tools/`

## 没搬什么

- 插件壳：`index.js`、`cordis.patch.yml`、`locale/`、`icon.svg`、`package.json`（OpenCode 没有对应机制）
- 上游仓库 CI 专用：`tools/ci_check.py`、`tools/selftest_noscipy.py`

## 与上游的差异（重新 vendor 时按这份清单重放）

1. **目录布局**：`schemas/`、`tools/` 从包根挪进技能目录。`scripts/selfcheck.py`、`tools/*.py`、`scripts/qaasr.py`
   里按「技能上两级 = 包根」写的路径改成「技能目录根」。`qaasr.py` 的 `assets/models` 路径同时修掉了上游
   的 `../..` 多一层（上游注释写的是 `<repo>/skills/video-studio/assets/models`，实际解析到了 `skills/assets`）。
   另外 `selfcheck.py` 第 22 条的结果行用 `os.path.relpath(web, os.getcwd())`，在「技能在 C:、工作目录在 D:」
   时抛 `ValueError`（跨盘符）——已加退化分支，改用绝对路径。
2. **提问工具**：`ask_user_question` → OpenCode `question`。
3. **看图工具**：`read_image` → `read`（OpenCode 的 `read` 直接返回图片）。
4. **设计台账默认路径**：`~/.dsh/video-studio/design-ledger.json` → `~/.config/opencode/video-studio/design-ledger.json`
   （`VIDEO_STUDIO_LEDGER` 环境变量与 `--no-ledger` / `--show-ledger` 行为不变）。
5. **路径占位符**：`<REPO>` 全部改成 `<SKILL_DIR>`；`<SKILL_DIR>` 的定义改成 OpenCode 的全局技能目录。
6. **`ci_check.py` 引用**：删掉「仓库级体检」一节，改成 selfcheck 是唯一可用性判据。

## 重新 vendor

从上游目标版本复制，然后重放上面的 1–6 条；改完必须跑：

```powershell
python $env:USERPROFILE\.config\opencode\skills\video-studio\scripts\selfcheck.py
```

本仓库收编时实测 **PASS 85 / FAIL 0 / SKIP 0**（Windows 11、Python 3.12、ffmpeg 9.0.2 full build、numpy 2.5、Pillow 13——注意 `requirements.txt` 的 Pillow 上界是上游验证口径 `<13`，实测 13.0.0 仍全过，不必降级）。
