# 第三方组件与改编声明

本项目以 **MIT** 许可发布（见 [LICENSE](LICENSE)）。下面是运行期需要留意的东西。

## 1. 算法思想改编（非代码拷贝）

`skills/video-studio/scripts/` 下四个模块（sfxkit / scorekit / webengine / qa_frames）的**算法思想**改编自开源项目
[ai-history-film](https://github.com/Re-Summer75/ai-history-film)（**MIT**，作者 Re-Summer75）：

| 本项目的文件 | 改编自 | 改编内容 |
|---|---|---|
| `sfxkit.py` | `sfx/`（core, synth, filters, modal, texture, space, dynamics, analysis, master, recipes） | 模态合成的物理模型（材质损耗因子、形状模态比、接触力脉冲）、泊松/Cox 事件纹理、镜像声源混响、BS.1770 响度与块级动态处理 |
| `scorekit.py` | `video/audio/film_score.py`、`inst.py` | 一张和弦表贯穿全片的编排思路、分段能量与律动层、重击前"吸气"、段落边界对齐小节线（只保留这些合成原语，不提供曲风表） |
| `webengine.py` + `assets/web/` | `video/render.py`、`video/web/js/engine.js` | 无头浏览器逐帧 seek + CDP 截图的确定性渲染流程、分块并行与断点续跑、音效提示点（cue）导出 |
| `qa_frames.py` | `video/qa_sync.js`、`video/qa_text.js`、`docs/开发日志.md` | 单帧闪烁的分块判据、画面空窗检测、音画事件同步对照、页面文字重叠/出画/一闪而过检查 |

**说明**：以上均为"读懂思路后用本项目约束重写"，**没有复制上游文件**，也没有引入上游的任何第三方依赖。
本项目未使用也不分发 three.js、GSAP（含 CustomEase / SplitText / DrawSVGPlugin）、Inter / JetBrains Mono / Noto Sans SC 字体文件——
浏览器渲染模板 `assets/web/` 只用原生 Canvas2D 与系统字体，因此不产生额外的许可义务。

## 2. 运行期依赖（不随仓库分发）

| 组件 | 用途 | 许可 | 必需？ |
|---|---|---|---|
| FFmpeg | 编码 / 解码 / 响度测量 / 黑帧检测 | LGPL-2.1+ / GPL（取决于构建） | **必需** |
| Python 3.9+ | 运行时 | PSF-2.0 | **必需**（本仓库 `tools/ci_check.py` 用 `ast.parse(feature_version=(3,9))` 校验了 3.9 **语法**兼容；作者机器实际运行在 3.12，未做 3.9 运行期实测） |
| numpy | 全部数值计算 | BSD-3-Clause | **必需** |
| Pillow | PIL 引擎逐帧绘制、联系表、深检图 | HPND | **必需**（PIL 引擎） |
| edge-tts | 旁白合成（走微软在线语音服务） | **LGPLv3**（客户端；实测 7.3.1 的分类器为 `License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)`）；**服务本身另有条款** | 有旁白时必需 |
| opencv-python（cv2） | 编码锐度比（Laplacian 方差） | Apache-2.0 | 可选（缺了跳过锐度门禁） |
| scipy | sfxkit 的时间域滤波加速 | BSD-3-Clause | 可选（缺了走 FFT 卷积兜底） |
| playwright | web 引擎驱动 Chromium/Edge | Apache-2.0 | 可选（只在 `--engine web` 时需要） |
| faster-whisper | ASR 回读门禁（G12）把成片音轨转回文字 | MIT（模型权重另有许可，随模型分发） | 可选（缺了降级到 ffmpeg whisper 滤镜 / skipped） |
| Chromium / Microsoft Edge | web 引擎的渲染内核 | BSD-3-Clause / 微软条款 | 可选 |

**字体**：PIL 引擎使用系统自带中文字体（Windows 的 `msyh.ttc`/`msyhbd.ttc` 等），
字体文件**不随本仓库分发**；能否商用请自行确认字体的许可。web 引擎的页面模板默认回落到系统字体。

## 3. 内容许可提醒

`ai-history-film` 的**代码**是 MIT，但它的**成片与 `docs/` 文档**是 CC BY-NC-SA 4.0（不可商用）。
本项目只借用了代码里的算法思路，**不包含**任何来自该片的画面、音频、文本或图片素材。
