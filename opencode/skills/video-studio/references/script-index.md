# 脚本资产索引（`SKILL.md` §10 的完整版）

> 长表从 `SKILL.md` 挪到这里：正文只保留"哪个脚本干什么"，参数细节与契约在这里，
> 而**各自的 `--help` 永远比文档新**——两者不一致时以 `--help` 为准，并顺手改这一篇。

约定：所有脚本都在技能目录里按路径直接调用，**不要拷进工程目录**（零拷贝，见 `SKILL.md` §1）。
`<SKILL_DIR>` = 本技能所在目录（OpenCode 的全局技能目录 `~/.config/opencode/skills/video-studio`，见 `SKILL.md` §1）。
`tools/`、`schemas/`、`assets/`、`scripts/` 都在它下面——下面的路径一律按 `<SKILL_DIR>` 展开。

> **所有脚本都在技能目录里，直接按路径调用即可**（如 `python <SKILL_DIR>/scripts/propose.py`）。
> 不需要拷到工程目录——主路径是零拷贝（§1）。

| 文件 | 作用 |
|---|---|
| `scripts/intake.py` | **出片前问诊**：固定 8 题（题材/制作方式/时长/气质/画幅/声线/配乐/补充）→ `work/intake.json` + `work/intake.brief.txt`。任何题材的气质推荐项都是「明亮活泼」；第 2 题「制作方式」按题材给推荐（`--recommend` / 问诊单的 `recommended`+`recommend_reason`：真实影像→footage、数据/原理→draw、两边都占→hybrid），`auto` 档落地成具体产线并写进 `intake.json` 的 `mode`（chosen/resolved/route）；`--finalize` 按产线打印下一步命令（素材线给 `footage.py`，不走 `propose.py`）。首次 `propose.py` 会因它没答完而拒绝 |
| `scripts/propose.py` | **提案器**：`--brief` 出 N 个候选（各带静帧联系表 + 设计距离）/ `--revise` 重出 / `--diff` 对比 / `--confirm` 冻结 `work/design.json` / `--show` 查看；片间去重台账（`--no-ledger` / `--show-ledger` / `VIDEO_STUDIO_LEDGER`） |
| `scripts/matchstyle.py` | **hybrid 风格对齐**：抽素材帧（有 EDL 就只抽真会用到的镜头）→ 中位色相/饱和度/明度 + 明暗族 → 交给 `designkit._solve_palette` 解出同族调色板 → `--apply` 写 `design.json` 的 `visual.palette` 与 `footage.json` 的 `treatment.letterbox_color`；`--align-bars` 连画幅一起对齐。写盘前过 `theme_lint`，失败零写入退 2。**素材本身不上色**，只让图文段向素材看齐 |
| `scripts/designkit.py` | **设计 DSL**：`sample_designs`（确定性采样、两两 ≥2 维不同、配色色相分叉）/ `validate_design`（fail-closed）/ `design_to_plan` / `design_distance` / `subtitle_of`；CLI：`--sample` / `--lint` / `--list-palettes` |
| `scripts/previewkit.py` | **离线预览**：合成时间轴 + `grammar.plan_scenes` + 场景绘制 + 联系表，不联网、不合成旁白 |
| `scripts/grammar.py` | **场景语法**：按内容从 10 类构图里选 `type`，落 `work/storyboard.json`；`--help` 可查用法 |
| `scripts/voicekit.py` | **旁白声线/语气计划**：裸 edge voice id + 实测响度补偿表 + 6 档 tone；`--list` 查音色表与语气档 |
| `scripts/filmkit.py` | 生产内核：字体/字形自查/缓存排版/字幕安全区（读 `design.visual.subtitle`）/缓动/确定性随机/音频总线/**CueBus 提示点**/**立体声混音 mix_film**/TTS/时间轴/**hold 停留窗口**/FrameSink/合成/质检 |
| `scripts/render.py` | 编排器：**14 个 stage**（`style`/`tts`/`layout`/`cues`/`score`/`sfx`/`audio`/`frames`/`mux`/`qa`/`stills`/`accept`/`explain`/`all`）、两种引擎（`--engine pil|web`）、分块并行（`--jobs`）、断点续跑（`--resume`）、母版预览（`stills`）、`--batch` 批量、`--preview N` 前 N 秒试片、`--run-id` 运行标签、`--vmaf full|sample|off`、`--no-preflight`。**`--scenes` 缺省 = 剧本同目录 `scenes.py` → 技能内置 `assets/scenes.py`** |
| `scripts/gates.py` | **门禁账本**：pass / fail / skipped 三档、`out/QC-REPORT.md` 渲染、人工签核（绑内容指纹）、原子写 |
| `scripts/stagewatch.py` | **阶段新鲜度**：每 stage 记录输入/输出指纹到 `work/manifest.json`；上游一改，下游报过时并给出最小重跑命令 |
| `scripts/filmcheck.py` | **film.json 契约校验**：未知键提示「是不是想写 X」（以前静默忽略）、枚举与范围、跨字段（cue 引用的 id 必须存在）。`style` 键现在是 **inert 警告**（已废弃、不参与渲染），不会再误报「找不到 stylekit.py」 |
| `scripts/layoutkit.py` | **画布/安全区单一真源**：横竖屏两套 profile（尺寸、正文区、字幕带、字号档）都在这里，scenes / filmkit / designkit 共用同一份定义（自检 #73 守住"只能有一个真源"） |
| `scripts/critic.py` | **审美质检**：离线启发式 0-100 分（安全边距 / 明暗对比 / 色相分散 / 文字密度 / 重心平衡）+ `--vlm-cmd` 挂钩（prompt 走 stdin、stdout 收 JSON，本脚本不联网）。评 `proposals/rev*/<ID>/_sheet.png`；**不写 `proposal.json`**、不进 `--stage qa` 账本 |
| `scripts/studio.py` | **薄门面**：`studio.py <子命令>` 转调 intake/propose/render/critic/check/lint/contract/explain；未知参数原样透传，退出码等于目标脚本 |
| `scripts/selfcheck.py` | 回归自检：**97 条**针对真实缺陷的断言，不联网、约 10–25 秒出结论（可选模块缺失标 SKIP）。后半段的重点项：**59 逐 stage 可单独跑 / 60 brief 里的要求真的生效 / 61 字幕样式可定制（`visual.subtitle` 真的生效）/ 62 片内版式轮换（`layout_cycle`：换章即换骨架）/ 63 带约束的 brief 不会高概率采样失败 / 64 已移除 API 的 fail-closed（不许 NameError / 假绿 / 静默回落）/ 65 `design.audio` 的音量/音效参数真的落进 script / 66 片间去重台账（确认记账 / 提案避开 / 可关可查）/ 67 时长要求真的被接住（brief 里的「2 分钟 / 90 秒」→ duration_target：决定候选段落数 + 渲染对账，成片长度仍由实测旁白决定）/ 68 缩放取景的帧间稳定性（改回 NEAREST 直采立刻 fail）/ 69 ASR 掉分可定位（低召回必须给出 lost_top 与最差段落）/ 70 出片前问诊（固定 8 题，没答完首次提案必拦）/ 71 默认设计不画装饰细线（母题层/底纹/HUD 全关，按绘制调用断言）/ 72 前景不随镜头位移（1px 台阶回归）/ 91 hybrid 默认形态 / 92 素材预算与 F8 / 93 配色锚点与气质互斥（采样前点名）/ 94 场景代码进内容指纹 / 95 G7 字幕三态 / 96 G8 黑帧结构化 / 97 intake --check 只读** |
| `scripts/footage.py` | **素材线一条龙**（10 个 stage）：`probe/fetch/transcribe/locate/tts/edl/cut/mix/assemble/qa`；契约是 `footage.json`（`schemas/footage.schema.json`），画面治理走 `treatment`，门禁是 F1–F8；素材预算与 F8 见 footage.md §2.1 |
| `scripts/footagekit.py` | 素材线核心 I：`load_footage`（结构硬错误）/ `probe_file` / `fetch_all`（yt-dlp + 直链）/ `transcribe` / `locate` / `build_treatment_filter`（crop→scale→pad→color→fps，切镜与抽帧共用一份，避免"预览和成片不一样"） |
| `scripts/footageedit.py` | 素材线核心 II：`build_edl` / `check_edl`（越界与总长）/ `cut_shots`（按镜切 + 抽原声）/ `mix_tracks`（旁白 + 环境层 + 配乐，采样级叠加）/ `write_ass` + `assemble`（ASS 字幕按 PlayResY=1080 排版） |
| `scripts/footageqa.py` | 素材线门禁 F1–F8：EDL 合法性 / 烧录字幕残留 / 水印残留 / 音画等长 / 溯源与署名 / 旁白主导 / 联系表与签核指纹（几何判定，不看"像不像字"） |
| `scripts/preflight.py` | **渲染前资源闸门**：剧本结构 + 全部上屏文本逐字过字形检测（按字段选字重）+ 已有 TTS 音频可解析 + 字体可加载 + web 入口页存在。`render.py` 在 `frames` 之前调用，记门禁 `G0`；也可单独跑 `python preflight.py --script film.json [--json] [--strict]` |
| `scripts/qavmaf.py` | **全片 VMAF/SSIM 门禁**：参考帧由场景函数**实时重渲**（不是成片抽帧），rawvideo 喂 ffmpeg 与成片逐帧比；一次调用同时算 VMAF 与 SSIM；门禁 `G11` |
| `scripts/qaasr.py` | **ASR 回读门禁**：成片音轨转文字与剧本对账（字符级多重集召回 + 每句偏移）；引擎优先 faster-whisper，其次 ffmpeg whisper 滤镜，都不可用记 skipped；门禁 `G12` |
| `scripts/sfxkit.py` | **物理建模音效库**：模态合成/纹理/空间/动力学/母带 + 24 个配方 + `CUE_ALIASES` |
| `scripts/musicgen.py` | **参数化配乐编排器**：和弦进行/动机/鼓型/音色全可传参，现场生成 `music.wav`（**没有风格表**） |
| `scripts/scorekit.py` | **配乐合成原语**：和声/律动/吸气/自动电平/混音/响度测量/24-bit 落盘。它不提供曲风，只提供可复用的底层技术 |
| `scripts/webengine.py` | **浏览器逐帧渲染**：Edge/Chromium + CDP 截图、分块并行、崩溃重开、cue 导出、文字质检 |
| `scripts/qa_frames.py` | **帧级质检**：单帧闪烁/画面空窗/音画同步/亮度与响度曲线（JSON + PNG） |
| `assets/scenes.py` | **技能内置渲染器**（`--scenes` 的默认回退）：10 类构图绘制器 + `draw_frame` + **`cues()` 提示点钩子示范**，含缓存排版与预烘焙背景的正确写法。要改画面就把它拷成你自己的 `scenes.py` 再改，或直接参考它写 |
| `assets/film.json` | **测试夹具**（跑通全链 / 金帧 / 门禁回归用，**不是给用户抄的模板**；里面没有 `style` 字段，外观由 `work/design.json` 决定） |
| `assets/design_fixture.json` | **金帧基准设计**（像素回归绑它，不绑任何内置风格） |
| `assets/voice_loudness.json` | 音色响度补偿实测表（voicekit 与 designkit 共用，避免两处漂移） |
| `<SKILL_DIR>/schemas/design.schema.json` | **设计 DSL 契约**（draft-07 子集）：六块字段、`visual.subtitle` / `layout_cycle`、fail-closed 规则 |
| `<SKILL_DIR>/schemas/proposal.schema.json` | **提案契约**：候选 + 确认状态 + `designHash` / `scriptHash` |
| `<SKILL_DIR>/schemas/film.schema.json` | **film.json 的契约**（JSON Schema draft-07 子集）。可执行校验器见 `scripts/filmcheck.py`（schema 只描述字段，跨字段规则在脚本里） |
| `<SKILL_DIR>/tools/estimate.py` | **开渲前预演**：帧数 / 各阶段耗时 / 磁盘峰值 / 分块建议，`--json` 可机读。基准常量集中在文件顶部，换机请重测后回填 |
| `<SKILL_DIR>/tools/golden_frames.py` | **金帧像素回归**：渲固定帧号算 SHA-256 与基线 JSON 比对（`--write` 重录 / `--check` 校验）。同环境像素变了是 fail，换了机器（字体指纹不同）是 skipped——跨机不误报。**两组基线**：`--layout landscape\|portrait` 切整片时刻基线（`golden_frames.json` / `golden_frames_portrait.json`）；`--compose` 切构图层基线（`golden_frames_compose*.json`，按 scene_type 逐类渲一帧，覆盖夹具片没有的 `compare`/`process`/`chart`） |
| `<SKILL_DIR>/tools/silhouette_qa.py` | **剪影可区分门禁（G13）**：不同设计渲染出的画面骨架必须两两可区分，防止「换设计只是换色」 |
| `assets/web/` | **浏览器页面示例**：原生 Canvas2D 确定性时间轴、场景示范、文字质检脚本。**不是出片模板**：`--engine web` 需要你自己的页面（见 §3） |
| `requirements.txt` | **Python 依赖清单**：必需（numpy/Pillow/edge-tts）/ 强烈建议（cv2）/ 可选（scipy、playwright）三档，带注释说明「缺了会怎样」。`pip install -r requirements.txt` 即可 |

### 关键内置能力

| API | 用途 |
|---|---|
| `K.tts(...)` | 逐句合成；缓存 key = text+voice+rate+pitch 哈希；网络重试 4 次；可选尾静音裁剪 |
| `K.loudnorm_two_pass(...)` | 两遍响度标准化，返回可给 `-af` 的滤镜串 |
| `K.loudness(path)` | 返回 `{I, LRA, TP}`（真峰已开 `ebur128=peak=true`） |
| `K.AudioBus.music_bed(...)` | 连续配乐床（和弦交叉淡化，无接缝爆点） |
| `K.AudioBus.mix()` | 归一化 + 软限幅，打印叠加峰值，>0.97 报警 |
| `K.wrap_cn_cached / text_w_cached` | 场景每帧排版必用（实测单帧 12ms → 4.6ms） |
| `K.glyph_missing_chars / check_glyphs` | 缺字形自查（✓ × ↵ 会画成方块） |
| `K.write_ass(...)` | 直接输出 ASS，头里声明 PlayResX/Y=1920/1080 |
| `K.encode_quality(film, ref_img=...)` | 锐度比；参考帧必须来自场景函数，不能是成片抽帧 |
