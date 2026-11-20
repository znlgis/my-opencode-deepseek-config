# 素材剪辑线回归样例（只有配置与联系表）

这份样例来自一次真实出片：**《四渡赤水：一条河上的四次掉头》**，3 分 11 秒 / 1080p / -16.5 LUFS。

| 文件 | 是什么 |
|---|---|
| `footage.json` | 那次出片的契约（节选，6 句代表句）：源声明、治理配方、混音参数、镜头时间码 |
| `contact-sheet.jpg` | 那次出片的九宫格联系表（派生帧图，1280px / 约 180 KB） |

**这里没有影视素材本体**——素材是第三方版权内容，只在本机使用，不入库。
本地复现的步骤：

1. 自己准备素材，放到 `media/source.mp4`（或改 `sources[].path`）；
2. 按 `footage.json` 里的 `at` 时间码核对镜头（照 1983 年八一厂《四渡赤水》4K 修复版的时间轴填的）；
3. `python ../../../scripts/footage.py --footage footage.json --work work --out out/film.mp4 --stage all`；
4. `--stage qa --strict-qa` 应全 pass（F7 需要看过联系表后自己签核）。

## 这份样例守的是什么

- `footageedit.build_timeline` 的尾留白并镜规则（早期版本会导致成片比旁白短 1.55 秒）
- `footageedit.make_edl` 的越界检查（`at+len ≤ 源时长`，需要 `sources_info` 才生效）
- `footageedit.assemble` 的绝对路径 + `cwd` 字幕烧录（Windows 盘符冒号坑）
- `footageqa` 的 F2/F3 几何判定（`crop=1576:660:172:80` 对 `burned_subs y=750..824` 与
  `watermark x=1580..1896 y=16..60` 应判 pass；改成 `crop=1920:824:0:0` 应判 fail）

不需要素材就能跑的是 `python tools/footage_selftest.py`：它自己造合成素材，
把上面这些规则全部断言一遍（离线，约 30 秒）。
