# 浏览器逐帧渲染引擎（webengine.py + assets/web/）

> 第二条渲染路线：无头浏览器打开 Canvas 页面，逐帧跳到「帧号 / FPS」后截 PNG，
> 经 ffmpeg image2pipe 编成 H.264。适合粒子、发光、连续形变、复杂矢量这类 PIL 画不动的画面；
> 图文/数据/科普片的主体仍然推荐 PIL 路线（快 3–5 倍，调试成本低）。

## 1. 什么时候用

| 需求 | 选型 |
|---|---|
| 粒子场、发光、纸纹、连续形变、镜头内复杂矢量运动 | **webengine.py**（本模块） |
| 图文卡片、字幕、图表、数字滚动、简单动效 | PIL 路线（filmkit + scenes.py） |
| 需要 WebGL 着色器 | webengine.py（页面里自己写 WebGL，本模板只示范 Canvas2D） |

硬约束：**页面不许连网络**（不引 CDN、不用网络字体），file:// 直接打开就能跑；
**不许 vendor GSAP / three.js**，页面只用原生 Canvas2D + 引擎自带的十几行缓动。

## 2. 一句话工作流

    python webengine.py snap  assets/web/index.html --times 1,5,10      # 先看 3 张静帧定构图
    python webengine.py cues  assets/web/index.html --out work/cues.json # 导出 cue 给配乐/音效用
    python webengine.py full  assets/web/index.html --out out/film.mp4 --workers 2
    python webengine.py textqa assets/web/index.html                    # 文字质检

## 3. 页面契约（engine.js 必须导出到 window）

| 成员 | 类型 | 说明 |
|---|---|---|
| window.ready | bool | 场景建好、document.fonts.ready 完成、首帧画完才置 true |
| window.DURATION | number | 成片秒数；render_full 用它切块并校验时长 |
| window.FPS | number | 页面时间轴帧率（模板 30）；webengine 据此算 -r / -g |
| window.renderFrame(f) | async | 把时间精确跳到 f/FPS，画完并等一次 requestAnimationFrame 后 resolve |
| window.getCues() | array | 声音提示点：[{t, type, gain?, dur?, size?, pan?, src}]，t 为成片绝对秒 |
| window.getEvents() | array | 视觉事件（闪白/震屏/冲击环）：[{t, kind, ...}]，供 qa_frames 做音画同步对照 |
| window.qaText(t0,t1,step) | async | 文字质检，返回 {overlaps, offscreen, brief, never} |

页面内部还有 FK 命名空间（改场景时用）：

| 成员 | 作用 |
|---|---|
| FK.scene(id, start, dur, build) | 注册场景；S.update(fn) 逐帧画，S.cue(t,type,params) 登记声音，S.event(t,kind,params) 登记视觉事件 |
| FK.always(fn) | 跨全片的逐帧回调（背景层放最前，HUD 放最后） |
| FK.g | 绘图原语：text / rect / rrect / line / circle / ring / glow / flash / bgGradient / grid / vignette / particles / measure / layoutChars |
| FK.E | 缓动表：lin in2 out2 inOut2 in3 out3 inOut3 out5 outExpo inExpo inOutExpo outBack |
| FK.hash(...args) / FK.rng(seed) | 确定性随机（替代 Math.random） |
| FK.watch(key, 标签) | 声明「这段文字应该出现」，qaText 的 never 检查靠它 |
| FK.texts() / FK.drawnKeys() / FK.qaReset() | 质检内部用（qa_text.js 调用） |

## 4. 模板页面里示范了 5 件事

assets/web/film.js 是 12 秒 / 3 个场景的完整模板：

1. **逐字入场**（0–4s）：标题每个字从下方 90px 升起 + 淡入，错峰 0.075s（E.out5）。
2. **数字滚动**（4–8s）：0 → 1920，走 out5 缓动，同步一条进度条。
3. **粒子 / 发光**（8–12s）：150 个粒子位置 = hash(seed,i,k) + 时间，中心双层辉光 + 冲击环。
4. **HUD 标注**（贯穿）：左上品牌/章节、右上时间码（每帧都变，天然保证"不同帧不同图"）、顶部进度线。
5. **cue / event 登记**：9 个 cue、12 个视觉事件，经 window.getCues()/getEvents() 导出。

改画面只需要改 film.js：把 S.update 里"帧号 → 画面"的表达式写清楚即可，不要读上一帧状态。

## 5. Python API（契约 §5.3 冻结）

    FPS = 30

    open_page(pw, url, width=1920, height=1080, log=False) -> (browser, page, cdp)   # async
    snap(html_path, times, out_dir, tag="") -> list[Path]          # 同步；多帧时自动拼联系表
    export_cues(html_path) -> dict                                  # §4.2 schema，可直接写 work/cues.json
    render_full(html_path, out_mp4, duration, *, workers=2, chunk_s=30.0,
                resume=True, from_s=0.0, to_s=None, crf=17, preset="slow") -> Path
    qa_text(html_path, t0=0.0, t1=None, step=0.2) -> dict
    selftest() -> int

补充约定：

- open_page 是 async 协程（与上游 ai-history-film 一致），同步调用请用 snap/render_full/export_cues。
- render_full 的 duration 传 None 时读页面 window.DURATION；传数字但与页面对不上会直接 raise。
- export_cues 返回 {"version":1,"duration","fps","width","height","cues":[...],"events":[...]}，
  除契约要求的 duration/cues/events 外补齐了 §4.2 文件头字段，写出去就是合法的 work/cues.json。
- 所有公开函数可独立调用：不依赖当前目录、不依赖"上一次调用留下的状态"。

## 6. 渲染管线

    [页面 window.renderFrame(f)] → address CDP Page.captureScreenshot(png, optimizeForSpeed)
        → PNG bytes → ffmpeg -f image2pipe -c:v png → libx264 → cXXX.part.mp4 → rename cXXX.mp4
    ... 多个 worker 从队列里领块并行 ...
    → 缺口检测 + 块时长校验 → filmkit.concat_segments（写绝对路径）→ 成片 mp4

关键细节：

- **显式 -r FPS、-g 2*FPS**：帧率写死，关键帧密集，便于逐段抽检。
- **块命名 c{起始帧//每块帧数:03d}.mp4**：按绝对帧号命名，重跑/改块大小都不会错位。
- **先写 .part.mp4 再 rename**：中断只会留下 .part 垃圾，不会把半截文件当成已完成的块。
- **已存在的块直接跳过**（断点续跑）：删掉某一块重跑，只会补那一块（实测 4s/2 块时只重渲 60 帧）。
- **拼接前两道门**：块缺失 → raise（拒绝少一段的片子）；块时长与「帧数/FPS」差 > 0.15s → raise。
- **崩溃重开**：单帧 90s 超时或页面异常 → 关浏览器重开，同一帧最多重试 5 次，仍失败 raise。
  逐帧渲染是无状态的，重开后从同一帧继续即可，不影响成片。
- 块目录结构（可整目录删掉强制重渲）：

      out/
        film.mp4
        chunks/
          film_0-12/
            c000.mp4  c001.mp4  c002.mp4  ...
            concat.txt
            _plan.json        # 渲染计划指纹（页面资源/帧率/区间/块长/crf/preset/画布/style）

断点续跑会比对 `_plan.json`：页面 JS/HTML 改了、帧率或块长/crf/preset 变了，
旧块**一律拒绝复用**（报错并指路「删块重渲」或 `--no-resume`）。
这条是 v2.7.3 修的真 bug：以前只按「块文件存在」复用，改了页面画面仍把旧块静默拼进新片。

## 7. 确定性守则（逐帧渲染的命门）

同一帧号必须画出同一张图，否则分块并行/断点续跑/重渲都会出现"帧间闪烁"。模板已经守住：

1. 不碰 Math.random / Date.now / performance.now；随机数走 FK.hash / FK.rng(seed)。
2. 不读上一帧状态：每帧清空画布、从头按 T 重画（不要写"每帧位移 += speed"这种累加）。
3. 字体固定：canvas 内 fallback 到 "Microsoft YaHei", sans-serif；文档字体加载完才 ready。
4. 视口默认 1920x1080、devicePixelRatio=1；画布 CSS 尺寸与分辨率一致，截图才是 1:1 像素。
   （`style/width/height/fps` 可让画布跟着交付规格走——只允许 **16:9 横屏或 9:16 竖屏的偶数尺寸**，
   4:3 / 奇数 / 非正数在 `open_page` 入口就 raise；尺寸由 `window.__STYLE__.canvas` 注入，
   `engine.js` 用同一套白名单，payload 的 `canvas.aspect` 按真实画布写 `16:9` / `9:16`。）
5. 动画相位只用绝对时间 T（如 Math.sin(T*2)），不要用帧计数差或挂钟。
6. 离屏缓存（发光精灵）在 boot 阶段建好，之后只读不写。
7. 质检项：自检里"同一帧两次截图 byte 完全相同"就是这条守则的守门员。

## 8. 性能与调参（本机实测：AMD 核显 / 12 线程 / Edge headless）

| 项 | 实测 |
|---|---|
| 1920x1080 PNG 截图 + 页面绘制 | 约 70–120 ms/帧（2 workers 时 70–85 ms/帧均摊） |
| 12 秒 / 360 帧 / 2 workers / chunk 3s | 28.0 s 出片（约 78 ms/帧均摊） |
| 自检（**36 项**，含真实无头浏览器、跨会话确定性验证、风格注入与竖屏画布断言） | 约 30–60 s |

调参顺序：先降 workers（1–2 个足够，多了内存涨），再降 chunk_s（块小重渲代价低），
最后才动 crf/preset（crf 17 / preset slow 是质量档；预览可用 crf 20 / preset veryfast）。
PNG 截图比 JPEG q94 慢但无压缩伪影，本模块按契约固定 PNG。

### 8.1 跟随设计（`window.__STYLE__`）

`--engine web` 的画风由「`style` plan 驱动」：`open_page` 在 `page.goto`
**之前**用 `add_init_script` 注入 `window.__STYLE__`（主题色板 / 版式 / 字体栈 / 画布 `w·h·fps`），
`assets/web/engine.js` 与 `film.js` 优先读它、取不到就退回页面内置的深蓝橙常量。

| 情形 | 行为 |
|---|---|
| `style=None`（pil 路线 / 显式关闭） | **连 `add_init_script` 都不调用**；页面里不存在 `window.__STYLE__`，像素与不带注入时逐字节一致（自检有断言） |
| 页面不认 `__STYLE__`（老 `engine.js`） | **当场报错**并给出入口页与 grep 提示，不能"截图能出图"就当通过 |
| 注入的画布尺寸与页面实际画布不一致 | 报错（页面没按 `panel` 建画布）；`fps` 注入了却与 `window.FPS` 不一致也报错 |
| 画布比例 | 只收 **16:9 / 9:16 的偶数尺寸**（`_validate_canvas` 与 `engine.js` 同一份白名单）；4:3 / 奇数 / 非正数在入口就 raise |
| 竖屏（9:16）画布 | 能建出来、能截图、文字不出画（自检有真起浏览器的 1080x1920 用例）。但内置模板的坐标是 1920x1080 设计坐标，竖屏下 `film.js` 的 X/Y 缩放不再是等比——版面是"横屏构图纵向拉长"，**不是竖屏原生重排**；要原生竖屏版式请用 `--engine pil --layout portrait` |

> 为什么要在 goto 之前注入：`engine.js` / `film.js` 是同步 IIFE，加载时就要读到 `__STYLE__`，
> 页面开始跑之后再注入就晚了。

## 9. 文字质检 qaText

    r = qa_text("assets/web/index.html", 0, 12, 0.2)
    # {"overlaps":[...], "offscreen":[...], "brief":[...], "never":[...], "frames": 61, "texts": 507}
    # frames = 实际采样帧数（12 s / step 0.2 -> 61）；texts = 扫描期间累计可见文本实例数
    # （同一段文字被多个采样点读到会重复计数，所以它远大于画面上的文字条数，别拿它当字幕条数）。

判据（改编自 ai-history-film（MIT）video/qa_text.js）：

| 项 | 判据 |
|---|---|
| overlaps | 两段文字的包围盒重叠面积比 ≥ 0.08（同一 group 内的逐字排版不算） |
| offscreen | 包围盒越过 1920x1080 边界 6px 以上 |
| brief | 可见时长 < 0.6s（一闪而过） |
| never | 页面用 FK.watch(key, 标签) 声明过、但整段扫描期间从未露面的文字 |

工作方式：qaText 每隔 step 秒调用一次 renderFrame，读回 engine.js 登记的文字包围盒与透明度
（文字必须用 FK.g.text 画，直接 ctx.fillText 不会进登记表）。扫描期间会反复改页面时间轴状态，
跑完需要成片截图时请重新 renderFrame 或重开页面。CLI 的 textqa 在四类缺陷中任意一类非空时
返回 exit 1（重叠/出画是布局错，一闪而过/从没露面同样会让成片看起来"漏字"，所以都算没通过）。

## 10. 和别的模块怎么接

- **配乐 / 音效**：webengine.py cues 输出 work/cues.json（§4.2），musicgen / sfxkit 按 t 对齐；
  页面 cue 的 type 用 sfxkit 配方名（ui_blip / whoosh / impact / data_chirp ...）。
- **音画同步质检**：页面 S.event 登记的视觉事件进 work/cues.json 的 events，qa_frames.sync_check 用。
- **合成成片**：webengine 只出无声画面；配音/配乐/混音由 filmkit 音频总线和 render.py 的 mux 阶段负责。
- **PIL 路线**：**本仓库不支持按段切引擎**（`--engine` 是全局单选项）。真要混用，得手工分别渲染两段，再用 `filmkit.concat_segments` 拼接，只要 fps/分辨率/时长一致，
  都用 filmkit.concat_segments 拼。

## 11. 故障排查

| 现象 | 根因 | 修法 |
|---|---|---|
| 等了很久 ready 不置 true | 页面脚本报错 / 字体 await 卡住 | page.on("pageerror") 打开 log=True 看错误；确认 FK.boot() 被调用 |
| 同一帧两次截图不同 | 页面用了 Math.random / Date.now / 累加位移 | 按第 7 节确定性守则改 |
| 截图是黑的 | 画布尺寸与 viewport 不一致 / 首帧还没画 | 画布 1920x1080、CSS 同步；ready 之后才截图 |
| 块渲染一半失败 | 浏览器崩溃 / 单帧超时 | 自动重开重试（最多 5 次）；仍失败看 [页面异常] 日志 |
| 拼接报「分块缺口」 | 块没渲完 / 块目录被手动删 | 重跑 --resume 补齐；或删块目录重渲 |
| 拼接报「块时长异常」 | 旧参数残留 / 块损坏 | 删对应块重渲（或 --no-resume 全重渲） |
| 成片比页面短 | 渲染区间 to_s 截断 | 检查 --from/--to；duration 与页面 DURATION 是否一致 |
| 中文变成方块 | 系统缺中文字体 | 改用系统已有字体（Windows 用 Microsoft YaHei）；要自带字体就放到**你自己项目**的 `web/fonts/` 并在页面里用 `@font-face` 引用（`assets/web/` 是只读模板，别往里放东西） |
| 渲染速度慢 | workers 太多/截图慢 | 2 个 worker 通常最优；确认 --enable-gpu 生效；必要时 crf 20 + preset veryfast 预览 |

## 12. 版权

- 逐帧 seek、分块并行、崩溃重开、断点续跑的算法改编自 ai-history-film（MIT）的 video/render.py；
- 页面场景组织（逐字入场 / 计数器 / HUD / cue）改编自其 video/web 与 video/qa_text.js；
- 本目录代码全部用原生 Canvas2D 重写，不含任何 GSAP / three.js 代码或构建产物。
