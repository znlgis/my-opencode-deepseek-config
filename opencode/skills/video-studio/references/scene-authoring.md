# 场景编写指南

## 1. 契约

```python
def draw_frame(scene, t_local, dur, t_global, script, frame) -> PIL.Image
```

| 参数 | 含义 | 用途 |
|---|---|---|
| `scene` | timeline.json 里的一段（含 id/text/sub/start/end/dur，以及你在 film.json 里写的所有字段） | 取本段专属内容（big/items/chapter） |
| `t_local` | 本段内已经过秒数 | 段内动画进度 |
| `dur` | 本段总秒数 | 归一化 `u = t_local/dur` |
| `t_global` | 全片秒数 | 全局 HUD、进度线 |
| `frame` | 全片帧号 | 确定性随机种子 |

返回值必须是与 `(1920,1080)` 同尺寸的 RGB `Image`。

## 2. 确定性铁律

```python
# 禁止
import random, time
x = random.random()
y = time.time()

# 允许
import filmkit as K
x = K.hash2(frame, 7)                  # 同一帧永远同一值
y = K.vnoise(t_local * 0.7, seed=3)    # 平滑噪声
```

**为什么**：断点续渲、按帧段并行渲染、事后重渲单帧，都依赖"同一帧号 = 同一张图"。用了 `random` 这些能力全部失效。

## 3. 性能：三层分离

```python
_BG_CACHE = {}

def _background():
    """静态部分只画一次，缓存复用"""
    if "bg" not in _BG_CACHE:
        _BG_CACHE["bg"] = make_gradient_and_texture()
    return _BG_CACHE["bg"]

def draw_frame(scene, t, dur, tg, script, frame):
    img = _background().copy()           # 1) 静态层
    img = draw_dynamic(img, t, frame)    # 2) 动态层
    img = draw_hud(img, tg)              # 3) UI 层
    return img
```

单帧预算：1080p30 时 ≤ 25 ms（PIL 纯 CPU）才能实时；实际项目 40 fps 是可用的（30 秒片子 ≈ 23 秒渲染）。

**常见性能坑**：
- 每帧 `ImageFont.truetype(...)` → 用 `filmkit.font(size, weight)`（带缓存）
- 每帧创建大渐变 → 缓存成图层 paste
- 每帧对全屏做 `GaussianBlur` → 预渲染模糊精灵
- 每帧 `np.array(img)` 来回转 → 只在写 sink 时转一次

## 4. 构图规则（中文视频）

```
画面分区（1920x1080）：
+------------------------------------------+ y=0
| 章节 chip (64,40)          片名 (右对齐) |
|                                          |
|  kicker 小标题 y=250                     |
|  大标题 84-96px  y=300  <- 左栏 x=150    |  右栏 x=1030
|  副标题 34-38px  y=400                   |
|                                          |
+------------------------------------------+ y=872  字幕带开始
|            字幕（<=2 行，居中）           |
+------------------------------------------+ y=1080
```

- **左重右轻**：左侧标题 + 右侧卡片/图示，或上标题下内容
- **留白不少于 25%**：塞满的中文画面会让人窒息
- **每屏一个视觉重心**：要么大字，要么图示，别两个都抢
- **中文行高** 1.3–1.4 倍字号；中英混排时长英文加空格
- **数字用等宽字体**（consola.ttf），滚动时不会抖

## 5. 转场与节奏

| 手法 | 实现 | 适用 |
|---|---|---|
| 元素入场 | `p = ease_out(rng(t, t0, t1)); y = base + 26*(1-p)` | 几乎每屏 |
| 卡片逐个出现 | 每条错开 0.3 s：`rng(t, 0.45+0.32*i, 1.15+0.32*i)` | 列表 |
| 数字滚动 | `v = int(target * ease_out(p))` | 数据页 |
| 镜头缓推 | `zoom = 1 + 0.04*u` | 整段 |
| 段落淡出 | 末 0.55 s `alpha = 1 - ease_in_out(rng(t, dur-0.55, dur))` | 段落切换 |

节奏底线：**每 3–6 秒必须有一次可见变化**；静止超过 6 秒观众会走。

## 6. 视觉质感（让 PIL 画面不像 PPT）

```python
# a) 竖直渐变底（不要纯色）
def vgrad(w, h, top, bot) -> Image

# b) 网点纹理（极低对比）
# c) 暗角（让视线聚焦中心）
def vignette(img, strength=0.35) -> Image

# d) 软边光点（比硬圆好看十倍）
#    椭圆 + GaussianBlur(r*0.18)，预渲染成精灵

# e) 半透明卡片 + 描边（1px 描边只给装饰型母题用；素净默认是无描边填充）
d.rounded_rectangle([...], radius=14, fill=(255,255,255,16), outline=(255,255,255,28))

# f) 顶部强调色进度条：画成块（6px），不要 1~3px 的细线
d.rectangle([0, 0, W * progress, 6], fill=ACCENT)
```

配色模板（深色科技风，久看不累）：
```python
bg_top  = (8, 16, 28)
bg_bot  = (18, 34, 56)
ink     = (238, 244, 252)
muted   = (150, 174, 200)
accent  = (243, 132, 60)     # 橙
accent2 = (86, 160, 230)     # 蓝
good    = (108, 178, 110)
bad     = (226, 96, 92)
```

浅色纸感风（纪录片/出版风）：
```python
paper   = (242, 233, 210)
ink     = (34, 30, 24)
accent  = (176, 48, 40)      # 朱红
muted   = (120, 108, 88)
```

## 7. 中文排版细节

```python
f = K.font(46, "reg")
w = K.text_w("一句话", f)              # 真实像素宽
lines = K.wrap_cn(text, f, 1560)       # 按像素折行，标点不落地行首
```

- 不要用 `len(s) * size` 估宽：中英文宽度差 2 倍
- 折行后必须检查：`K.check_bbox(box, K.SAFE, tag="标题")`
- 标点悬挂：`，。、；：？！》"’` 不出现在行首（`wrap_cn` 已处理）

## 8. 浏览器渲染方案（需要复杂视觉时）

当需要 WebGL 着色器、复杂粒子、纸纹合成时：

```python
# 逐帧：把时间轴精确跳到 帧号/fps，截图，送 ffmpeg
await page.evaluate("f => window.renderFrame(f)", frame)
png = await cdp.send("Page.captureScreenshot",
                     {"format": "jpeg", "quality": 94, "optimizeForSpeed": True})
ff.stdin.write(base64.b64decode(png["data"]))
```

启动参数：
```
--use-angle=d3d11 --enable-gpu --ignore-gpu-blocklist
--force-color-profile=srgb --hide-scrollbars --disable-lcd-text
--font-render-hinting=none
```

实测要点（核显 AMD 890M / 12 核；**下面 JPEG 那组是上游 ai-history-film 的经验，
本仓库 `webengine` 固定用 PNG**——参考它只在你自己写截图器时有用）：
- 上游：PNG 截图 119 ms/3MB vs **JPEG q94 30 ms/192KB**（快 4 倍，PSNR 41–44 dB 肉眼看不出）
- 上游端到端 215 ms/帧 → 55 ms/帧；**本仓库实测**：PNG 逐帧约 80–130 ms/帧（1080p30，Edge 无头）
- **复用浏览器会话**，不要每块开新页（进程会堆到 28 个、内存 2.7 GB 后卡死）
- 分块按**绝对起始帧**命名（`seg_000000_000120.mp4`），拼接时显式检缺口

## 9. 超采样（提升线条质感）

```python
SS = 2.0                       # 内部 3840x2160 渲染，最后缩到 1920x1080
canvas = Image.new("RGB", (int(W*SS), int(H*SS)))
...
out = canvas.resize((W, H), Image.LANCZOS)
```
代价：4 倍像素 → 渲染慢 3–4 倍。只在对线条/文字质感要求极高时用。

## 10. 字幕的两条路线（都要会）

### 路线 A：画进帧里（推荐，完全可控）

`filmkit.draw_subtitle` 直接把字幕画到 PIL 帧上：安全区、背板、两行折行、字号自适应全部可控，不依赖外部工具，所见即所得。

优点：位置精确、样式自由、不会出现"播放器把字幕裁掉"。
缺点：改字幕要重渲（但只重渲受影响的段）。

### 路线 B：外挂/烧录 SRT/ASS（libass）

这条路有两个坑，必须记住：

**坑 1：SRT 交 libass 渲染，字号会小得看不见。**
libass 按 `PlayResY=288` 的虚拟分辨率排版，而你这个视频是 1080 高——**所有 FontSize / MarginV 要乘 3.75**（1080/288）。

```bash
# 正确：显式指定字号与边距（1080p 下 FontSize≈46*3.75≈172 太大，实际按需求调）
ffmpeg -i in.mp4 -vf "subtitles=sub.srt:force_style='FontName=Microsoft YaHei,FontSize=54,MarginV=60'" out.mp4
```

更稳的做法是**直接用 ASS**，在文件头写明真实分辨率：

```
[Script Info]
PlayResX: 1920
PlayResY: 1080
```
这样 FontSize 就是真实像素，不用换算。

**坑 2：ASS 的 Format 行漏字段 → 位置全乱。**
`[Events]` 区的 `Format:` 行字段顺序必须与每条 `Dialogue:` 完全对应。少写 `MarginL,MarginR,MarginV` 就会整片错位。

### 880 硬规则

字幕带以下（`y > 880`）**不要放任何正文元素**。真实项目里反复出现过"元素被字幕挡住 / 字幕压住图表"。

⚠ **这里没有"天然满足"**：`filmkit.SAFE` 的 y1 = 974 是**硬边界**（超了才报错），
而字幕带 `SUB_BAND` 从 872 开始——两者重叠 102 px，正文画进 872–974 就会被字幕压住。
真正该守的是：**正文实际排版止于 ~860**（给字幕带留 12 px 间隙）。
`SAFE` 只负责"别画出画面"，不负责"别被字幕挡住"。

### 中文符号字形缺口

微软雅黑粗体（msyhbd）**缺 ✓ × ↵ 这些符号**，画出来是方框。要打勾/打叉请用 `ImageDraw` 画两条线：

```python
def draw_check(d, x, y, s=14, color=(108,178,110), w=4):
    d.line([(x, y+s*0.5), (x+s*0.45, y+s)], fill=color, width=w)
    d.line([(x+s*0.45, y+s), (x+s*1.2, y-s*0.2)], fill=color, width=w)

def draw_cross(d, x, y, s=12, color=(226,96,92), w=4):
    d.line([(x, y), (x+s, y+s)], fill=color, width=w)
    d.line([(x+s, y), (x, y+s)], fill=color, width=w)
```
