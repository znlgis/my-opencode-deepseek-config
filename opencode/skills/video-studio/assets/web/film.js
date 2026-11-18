/* film.js — 场景模板（v0.8.0）
 *
 * 3 个场景 / 默认 12 秒（global.DURATION 由场景表推出，见下方 resolveDuration：
 * 加一幕、改一幕时长都不用手工同步常量；要锁死就写 window.WINDOW_DURATION）。
 * 这是**独立的 12 秒演示片**，不是"随便配一份 film.json 就能出全片"的成品页面：
 * 换剧本必须自己重写场景，让画面覆盖到片尾，再按需写 WINDOW_DURATION。
 *
 * 示范 5 件事：
 *   ① 逐字入场（0–4s）  ② 数字滚动（4–8s）  ③ 粒子 / 发光（8–12s）
 *   ④ HUD 标注（贯穿全片）  ⑤ cue / event 登记（由 window.getCues()/getEvents() 导出）
 *
 * v0.8.0 风格跟随（契约）
 * ----------------------
 * 色板 / 版式 / 材质强度 / HUD 装帧优先读 window.__STYLE__（webengine.open_page(style=...)
 * 在页面加载前注入）；取不到就用本文件里的 v0.7 常量。
 *   - 色板：theme.{bg,bg2,surface,ink,dim,accent,accent2,rule} 决定底色 / 正文 / 强调色；
 *   - 版式：layout.default ∈ center-stack / left-stack / split-right 决定标题块的锚点与对齐，
 *           hud ∈ chip-left / corner-line / none 决定 HUD 装帧；
 *   - 材质：material.vignette / texture_alpha 决定暗角与网格强度；
 *   - 动效：motion.entrance_dy 决定标题入场位移；motion.transition=wash 时转场用底色而不是黑；
 *   - 画布：canvas.{w,h,fps} 已在 engine.js 里生效；本文件用 X()/Y()/S() 把 1920x1080 设计坐标
 *           缩放到实际画布，1920x1080 时三者原样返回。
 *           16:9 尺寸（1280x720 等）下 KX=KY=KS，是等比缩放。
 *           **9:16 竖屏下 KX≠KY**：X() 压窄、Y() 拉长，标题/正文的字号走 S()=min 不糊，但
 *           版面是"把横屏构图拉长"，不是按竖屏重排——真要竖屏原生版式请用
 *           `--engine pil --layout portrait`（scenes.py 的十类构图都有竖屏实现）。
 *           模板页面按 1920x1080 设计，竖屏属于"能出片、不出画"的边界，自检只保证后者。
 * **没有 __STYLE__ 时**，上面每条取到的都是 v0.7 原值，渲染像素与旧版逐字节一致（自检有证据）。
 *
 * 纪律：每帧都是「帧号 → 画面」的纯函数——不读上一帧状态、不用 Math.random / Date.now。
 * 画面里的所有文字都走 FK.g.text()，qaText 才能看到它们的包围盒。
 * 改编自 ai-history-film（MIT）video/web2 的场景组织方式（逐字入场 / 计数器 / HUD 的写法）。
 */
(function (global) {
  'use strict';
  var F = global.FK;
  if (!F) throw new Error('film.js: engine.js 未加载');
  var W = F.W, H = F.H, g = F.g, E = F.E;
  var seg = F.seg, lerp = F.lerp, clamp = F.clamp;

  /* ================================================================ 风格包 → 色板 / 版式 */
  var ST = (global.__STYLE__ && typeof global.__STYLE__ === 'object') ? global.__STYLE__ : null;
  var TH = (ST && ST.theme && typeof ST.theme === 'object') ? ST.theme : null;
  var MOTION = (ST && ST.motion && typeof ST.motion === 'object') ? ST.motion : null;
  var MATERIAL = (ST && ST.material && typeof ST.material === 'object') ? ST.material : null;

  /* 设计坐标 → 实际画布：1920x1080 时 KX = KY = KS = 1，三个函数原样返回（不做任何取整），
   * 所以旧页面（无 style）尺寸路径一个数字都不变。 */
  var KX = W / 1920, KY = H / 1080, KS = Math.min(KX, KY);
  function X(v) { return KX === 1 ? v : Math.round(v * KX); }
  function Y(v) { return KY === 1 ? v : Math.round(v * KY); }
  function S(v) { return KS === 1 ? v : Math.round(v * KS); }

  /* 颜色归一：接受 "#rrggbb" / [r,g,b]；解析不了返回 null（由调用点的兜底值接手）。 */
  function toRgb(c) {
    if (Array.isArray(c) && c.length >= 3) {
      var a = [Number(c[0]) | 0, Number(c[1]) | 0, Number(c[2]) | 0];
      if (a[0] >= 0 && a[0] <= 255 && a[1] >= 0 && a[1] <= 255 && a[2] >= 0 && a[2] <= 255) return a;
      return null;
    }
    if (typeof c === 'string') {
      var s = c.trim();
      var m = /^#([0-9a-fA-F]{6})$/.exec(s);
      if (m) { var n = parseInt(m[1], 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
      var m2 = /^rgba?\(([^)]+)\)$/.exec(s);         // 也认 rgb()/rgba() 字符串
      if (m2) {
        var parts = m2[1].split(',');
        if (parts.length >= 3) {
          return [parseInt(parts[0], 10) | 0, parseInt(parts[1], 10) | 0, parseInt(parts[2], 10) | 0];
        }
      }
    }
    return null;
  }
  function P(key, fb) { var a = TH ? toRgb(TH[key]) : null; return a || fb; }
  function rgbStr(a) { return 'rgb(' + a[0] + ',' + a[1] + ',' + a[2] + ')'; }
  function A(c, alpha) {
    var a = toRgb(c) || [255, 255, 255];
    return 'rgba(' + a[0] + ',' + a[1] + ',' + a[2] + ',' + alpha + ')';
  }

  /* HUD 小字的透明度：深色主题按 v0.7 的 alpha；浅色主题上"淡白字"会消失，统一乘一个系数。
   * 没有 __STYLE__ 时系数 = 1，A() 收到的 alpha 与旧版字面量完全一致。 */
  var HUD_BOOST = (ST && ST.dark === false) ? 1.6 : 1.0;
  function HA(alpha) { return A(C.ink, Math.min(1, alpha * HUD_BOOST)); }

  /* 底部色板：没有 __STYLE__ 时每一项都是 v0.7 常量（包括字符串写法，便于逐字节对比）。 */
  var C = {
    ink: TH ? rgbStr(P('ink', [234, 242, 255])) : '#eaf2ff',
    dim: TH ? rgbStr(P('dim', [142, 164, 192])) : '#8ea4c0',
    bright: TH ? rgbStr(P('ink', [246, 249, 255])) : '#f6f9ff',
    accent: P('accent', [255, 140, 66]),
    cyan: P('accent2', [90, 210, 255]),
    warm: P('accent2', [255, 196, 120]),
    gridAlpha: (MATERIAL && MATERIAL.texture_alpha != null) ? Number(MATERIAL.texture_alpha) : 0.045,
    vig: (MATERIAL && MATERIAL.vignette != null) ? Number(MATERIAL.vignette) : 0.5,
    /* 白闪：深色主题保持纯白；浅色主题用 ink（白闪在浅底上会"消失"，等于闪了一下没有） */
    flash: (ST && ST.dark === false && TH) ? rgbStr(P('ink', [255, 255, 255])) : '#ffffff',
    /* 转场收尾：transition=wash 用底色淡入，其余（含无 style）仍是黑场 */
    wipe: (MOTION && MOTION.transition === 'wash' && TH) ? rgbStr(P('bg', [11, 14, 20])) : '#000000'
  };

  var BG_STOPS = TH
    ? [[0, P('bg', [11, 14, 20])], [0.55, P('bg2', [14, 22, 36])], [1, P('bg', [10, 13, 19])]]
    : [[0, '#0b0e14'], [0.55, '#0e1624'], [1, '#0a0d13']];

  /* 版式：没有 __STYLE__ 时 HUD = 'default'（v0.7 原样）、LAYOUT = 'center-stack'。 */
  var LAYOUT = (ST && ST.layout && typeof ST.layout.default === 'string') ? ST.layout.default : 'center-stack';
  var HUD = (ST && typeof ST.hud === 'string') ? ST.hud : 'default';
  var ANCHOR = 'center';                       // center-stack 居中；left-stack 左；split-right 右
  if (ST) {
    if (LAYOUT === 'left-stack') ANCHOR = 'left';
    else if (LAYOUT === 'split-right') ANCHOR = 'right';
  }
  var MARGIN = X(168);
  /* 标题入场位移：无 style 时是 v0.7 的 90px；有 style 时按 motion.entrance_dy 放大 4 倍
   * （stylekit 里的单位是 PIL 排版像素，网页坐标要乘 4 才接近原来 90px 的观感）。 */
  var RISE = (MOTION && MOTION.entrance_dy) ? S(Number(MOTION.entrance_dy) * 4) : Y(90);

  function anchorX(width) {                     // 逐字排版用的左起 x
    if (ANCHOR === 'left') return MARGIN;
    if (ANCHOR === 'right') return W - MARGIN - width;
    return (W - width) / 2;
  }
  function place() {                            // 居中/左/右对齐的单行文字锚点
    if (ANCHOR === 'left') return { x: MARGIN, align: 'left' };
    if (ANCHOR === 'right') return { x: W - MARGIN, align: 'right' };
    return { x: W / 2, align: 'center' };
  }
  function blockX(blockWidth) {                 // 进度条 / 强调线这类"块"的左边界
    if (ANCHOR === 'left') return MARGIN;
    if (ANCHOR === 'right') return W - MARGIN - blockWidth;
    return (W - blockWidth) / 2;
  }

  /* 成片秒数（webengine.py / render.py 会读它做分块与校验）。
   * 默认由场景表推出 = 最后一个场景的 end（加/删/改场景后自动跟着变，不会忘记同步）；
   * 要显式锁死（例如页面里还有场景以外的动画），在 index.html 里先写
   *   <script>window.WINDOW_DURATION = 12.0;</script>
   * 或在引入 film.js 之前赋值即可覆盖。使用说明见 references/web-engine.md §3。 */
  function resolveDuration(F) {
    var explicit = Number(global.WINDOW_DURATION);
    if (isFinite(explicit) && explicit > 0) return explicit;
    // 也认"在 film.js 之前直接写 window.DURATION = 秒数"的写法：render.py 的报错就是
    // 教用户改 window.DURATION（页面里的时长必须等于 timeline.total）。
    // engine.js 自己写的兜底值不算显式，否则模板永远锁死在 12 秒，加场景也不生效。
    var pre = Number(global.DURATION);
    if (isFinite(pre) && pre > 0 && pre !== Number(F.defaultDuration)) return pre;
    return F.timelineEnd();
  }

  /* qaText 的「从没露面」检查要先声明 key：必须与 g.text({key}) 对齐。
   * hud='none' 的装帧不画品牌/章节文字，就别声明它们（否则 never 会误报）。 */
  F.watch('open.title', '标题：逐帧确定性');
  F.watch('open.sub', '副标题：Canvas 2D 极简时间轴');
  F.watch('data.num', '数字滚动：0 → 1920');
  F.watch('data.cap', '说明：同一帧号 → 同一张图');
  F.watch('fx.title', '标题：确定性渲染');
  F.watch('fx.hud', 'HUD：粒子数 / 帧率标注');
  if (HUD !== 'none') F.watch('hud.ch', 'HUD：章节名');
  F.watch('hud.tc', 'HUD：时间码');
  if (HUD !== 'none') F.watch('hud.brand', 'HUD：品牌名');

  /* ================================================================ 背景层（最先注册 → 最先画） */
  F.always(function (t) {
    g.bgGradient(BG_STOPS);
    g.grid({ t: t, step: X(120), alpha: C.gridAlpha, color: C.cyan, drift: S(6) });
    g.vignette(C.vig);
  });

  /* ================================================================ 场景 1：逐字入场（0–4s） */
  F.scene('open', 0, 4.0, function (S1) {
    // ⚠ 命名约定（见 references/frame-qa.md §2.5）：
    //   qa_frames 的音画同步**先按 kind == cue 的配方名配对**。
    //   所以每个"该响一下"的画面事件，kind 要写成**同一个配方名**（ui_blip / whoosh / impact …）；
    //   只有纯装饰性的视觉事件（交界黑场）才用 flash 这类通用名。
    //   底下每条 S.cue 都配了一条同名 S.event —— 这样同步检查才真的在查，而不是静默全过。
    S1.cue(0.10, 'ui_blip', { pitch: 2200, gain: -4 });
    S1.event(0.10, 'ui_blip', { a: 0.5 });
    S1.cue(0.34, 'ui_blip', { pitch: 2600, gain: -6 });
    S1.event(0.34, 'ui_blip', { a: 0.5 });
    S1.cue(1.05, 'whoosh', { dur: 0.55, size: 0.6, gain: -10 });
    S1.event(1.05, 'whoosh', { size: 0.6 });            // 强调线"画出来"的同时一声掠过
    S1.event(3.70, 'flash', { a: 0.18 });               // 4s 交界黑场起坡（纯视觉，无对应音效）

    var TITLE = '逐帧确定性';
    var SIZE = S(150);
    var TRACK = S(8);

    S1.update(function (t) {
      // ① 逐字入场：每个字从下方 RISE 升起 + 淡入，错峰 0.075s（RISE 跟随 style 的 motion.entrance_dy）
      var lay = g.layoutChars(TITLE, SIZE, '700', null, TRACK);
      var x = anchorX(lay.width);
      var y = Y(470);
      for (var i = 0; i < lay.chars.length; i++) {
        var p = E.out5(seg(t, 0.16 + i * 0.075, 0.72 + i * 0.075));
        var a = seg(t, 0.16 + i * 0.075, 0.44 + i * 0.075);
        g.text(lay.chars[i].ch, x + lay.chars[i].x, y + (1 - p) * RISE, {
          size: SIZE, weight: '700', color: C.ink, alpha: a,
          key: 'open.title.' + i, group: 'open.title'
        });
      }
      // 标题下方一条「画出来」的强调线（跟随版式锚点）
      var lw = E.out3(seg(t, 1.15, 1.9)) * X(360);
      g.rect(blockX(lw), y + Y(118), lw, S(4), A(C.accent, 0.9));
      // 副标题淡入 + 上浮
      var a2 = E.out3(seg(t, 1.35, 2.05));
      var ps = place();
      g.text('Canvas 2D · 极简时间轴 · 无 GSAP / three.js', ps.x, Y(646) + (1 - a2) * Y(26), {
        size: S(40), weight: '400', color: C.dim, align: ps.align, alpha: a2,
        key: 'open.sub', group: 'open.sub'
      });
      // 两侧装饰竖线（"长出来"）
      var dw = E.out3(seg(t, 1.6, 2.2));
      if (dw > 0.001) {
        g.rect(X(196), Y(430) + (1 - dw) * Y(60), S(2), Y(120) * dw, A(C.cyan, 0.55));
        g.rect(W - X(198), Y(430) + (1 - dw) * Y(60), S(2), Y(120) * dw, A(C.cyan, 0.55));
      }
    });
  });

  /* ================================================================ 场景 2：数字滚动（4–8s） */
  F.scene('data', 4.0, 4.0, function (S2) {
    // 同上：cue 与 event 成对出现、同名，同步检查才有东西可配
    S2.cue(0.10, 'data_chirp', { gain: -4 });
    S2.event(0.10, 'data_chirp', {});
    S2.cue(1.85, 'ui_click', { gain: -5 });
    S2.event(1.85, 'ui_click', {});                     // 数字滚完落定的一声"咔"
    S2.cue(2.12, 'relay_click', { gain: -12 });
    S2.event(2.12, 'relay_click', {});

    S2.update(function (t) {
      // ② 数字滚动：0 → 1920，走 out5 缓动（声音 chirp 与它同一条时间线）
      var p = E.out5(seg(t, 0.25, 1.85));
      var num = Math.round(lerp(0, 1920, p));
      var s = String(num);
      while (s.length < 4) s = '0' + s;
      var pn = place();
      g.text(s, pn.x, Y(470), {
        size: S(236), weight: '700', align: pn.align, color: C.bright,
        key: 'data.num', group: 'data.num'
      });
      g.text('FRAME-STABLE CUTS', pn.x, Y(606), {
        size: S(26), weight: '500', align: pn.align, color: A(C.cyan, 0.85),
        alpha: E.out3(seg(t, 0.9, 1.4)), key: 'data.kicker', group: 'data.kicker'
      });
      // 说明文字
      var a = E.out3(seg(t, 1.05, 1.75));
      g.text('同一帧号 → 同一张图', pn.x, Y(700) + (1 - a) * Y(20), {
        size: S(44), weight: '400', align: pn.align, color: C.dim, alpha: a,
        key: 'data.cap', group: 'data.cap'
      });
      // 进度条（数字滚动的可视化）
      var bw = X(720), bx = blockX(bw), by = Y(792);
      g.rect(bx, by, bw, S(3), A(C.ink, Math.min(0.5, 0.12 * HUD_BOOST)));
      g.rect(bx, by, bw * p, S(3), A(C.accent, 0.95));
      // ④ HUD 标注：右上角小徽标
      var ha = E.out3(seg(t, 0.5, 1.0));
      if (ha > 0.001) {
        g.text('◆ DATA LAYER', W - X(64), Y(206), {
          size: S(22), weight: '600', align: 'right', color: A(C.accent, 0.9),
          alpha: ha, key: 'data.hud', group: 'data.hud'
        });
        g.text('counter → 1920', W - X(64), Y(238), {
          size: S(18), weight: '400', align: 'right', color: HA(0.45),
          alpha: ha, key: 'data.hud2', group: 'data.hud'
        });
      }
    });
  });

  /* ================================================================ 场景 3：粒子 / 发光（8–12s） */
  F.scene('fx', 8.0, 4.0, function (S3) {
    S3.event(-0.30, 'flash', { a: 0.18 });   // 8s 交界黑场起坡（本幕负偏移 = 上一幕末帧）
    S3.cue(0.02, 'reverse_swell', { gain: -4, dur: 1.2 });
    S3.event(0.02, 'reverse_swell', {});                // 吸气 = 画面从黑里亮起来
    S3.cue(0.62, 'impact', { size: 1.15, gain: -1 });
    S3.event(0.62, 'impact', { a: 0.8 });               // 重击：白闪 + 冲击环同一时刻
    S3.event(0.62, 'ring', { r: 620 });                 // 纯装饰（无对应音效）
    S3.cue(1.55, 'ui_blip', { pitch: 1800, gain: -8 });
    S3.event(1.55, 'ui_blip', { a: 0.5 });

    S3.update(function (t) {
      // ③ 粒子 / 发光：位置全部由 hash(seed,i,*) 决定，同一 t 永远同一张图
      g.particles({ n: 150, seed: 20261107, t: t, color: C.accent, alpha: 0.5, speed: 1.0, rmin: S(2), rmax: S(6), wobble: Y(30) });
      // 冲击环：impact 时刻从中心扩散
      var rp = E.out5(seg(t, 0.62, 2.3));
      if (rp > 0.001 && rp < 0.999) {
        g.ring(W / 2, Y(520), lerp(S(40), S(640), rp), A(C.warm, (1 - rp) * 0.55), S(3));
      }
      // 中心发光：正弦呼吸，相位只由绝对时间 T 决定
      var T = 8.0 + t;
      g.glow(W / 2, Y(520), S(250), C.accent, 0.18 + 0.07 * Math.sin(T * 2.0));
      g.glow(W / 2, Y(520), S(130), C.warm, 0.10 + 0.05 * Math.sin(T * 2.6 + 1.2));
      // 标题逐字入场（复用同一套缓动）
      var lay = g.layoutChars('确定性渲染', S(132), '700', null, S(10));
      var x = anchorX(lay.width);
      var y = Y(520);
      for (var i = 0; i < lay.chars.length; i++) {
        var p = E.out5(seg(t, 1.05 + i * 0.07, 1.55 + i * 0.07));
        g.text(lay.chars[i].ch, x + lay.chars[i].x, y + (1 - p) * Y(60), {
          size: S(132), weight: '700', color: C.flash, alpha: p,
          key: 'fx.title.' + i, group: 'fx.title'
        });
      }
      // HUD 标注
      var pf = place();
      g.text('PARTICLES 150 · 30 FPS · 同一帧同一图', pf.x, Y(660), {
        size: S(28), weight: '400', align: pf.align, color: HA(0.66),
        alpha: E.out3(seg(t, 1.3, 1.9)), key: 'fx.hud', group: 'fx.hud'
      });
      // 白闪：impact 事件的可视化（event 已登记同一条时间；只从 impact 时刻开始衰减）
      var fp = seg(t, 0.62, 0.92);
      if (t >= 0.62 && fp < 0.999) g.flash(0.55 * (1 - fp), C.flash);
      // 片尾淡出到黑（wash 主题淡到自己的底色）
      var fo = E.in2(seg(t, 3.35, 4.0));
      if (fo > 0.001) g.flash(fo, C.wipe);
    });
  });

  /* ================================================================ 时间轴总长（DURATION） */
  /* 必须在所有 F.scene(...) 注册完之后求值：resolveDuration 读的就是场景表。
   * 放在 HUD 之前、注册之后，这样 HUD 里读 global.DURATION 时它已经是正确值（boot 在最后）。 */
  global.DURATION = resolveDuration(F);

  /* ================================================================ HUD 层（最后注册 → 最后画，压在所有场景之上） */
  F.always(function (t) {
    var dur = Number(global.DURATION) || 12.0;
    // HUD 装帧（只在注入 style 时启用；default = v0.7 原样）
    if (HUD === 'chip-left') {
      g.rrect(X(48), Y(36), X(300), Y(84), S(12), A(P('surface', [26, 42, 64]), 0.55));
    } else if (HUD === 'corner-line') {
      var cl = A(P('rule', [255, 255, 255]), 0.28);
      var m = X(40), ln = X(96), th = S(1);
      g.rect(m, m, ln, th, cl);
      g.rect(m, m, th, ln, cl);
      g.rect(W - m - ln, m, ln, th, cl);
      g.rect(W - m - th, m, th, ln, cl);
      g.rect(m, H - m - th, ln, th, cl);
      g.rect(m, H - m - th, th, ln, cl);
      g.rect(W - m - ln, H - m - th, ln, th, cl);
      g.rect(W - m - th, H - m - th, th, ln, cl);
    }
    if (HUD !== 'none') {
      g.text('DSH VIDEO STUDIO', X(64), Y(60), {
        size: S(24), weight: '500', color: HA(0.55), key: 'hud.brand'
      });
      var chap = t < 4 ? '01 · 逐字入场' : (t < 8 ? '02 · 数字滚动' : '03 · 粒子 / 发光');
      g.text(chap, X(64), Y(96), {
        size: S(22), weight: '500', color: A(C.cyan, 0.8), key: 'hud.ch', group: 'hud.ch'
      });
    }
    // 时间码每帧都在变 → 也保证「不同帧渲染结果不同」
    var tc = 'T ' + (t < 10 ? '0' : '') + t.toFixed(2) + ' / ' + dur.toFixed(2);
    g.text(tc, W - X(64), Y(60), {
      size: S(22), weight: '500', align: 'right', color: HA(0.45),
      key: 'hud.tc', group: 'hud.tc'
    });
    // 顶部进度线（HUD 装帧为 none 时也保留：它是"逐帧在动"的最直观证据）
    g.rect(X(64), Y(122), W - X(128), S(1), A(C.cyan, 0.16));
    g.rect(X(64), Y(122), (W - X(128)) * clamp(t / dur, 0, 1), S(2), A(C.accent, 0.85));
    // 底部品牌行（有 style 时写风格名，便于目检"这条片用的是哪套画风"）
    var foot = ST ? ('v0.8.0 · ' + (ST.style_name || ST.style || ST.theme_label || 'style') + ' · Canvas2D 逐帧渲染')
                  : 'v0.3.0 · Canvas2D 逐帧渲染 · 不使用 GSAP / three.js';
    g.text(foot, X(64), H - Y(52), {
      size: S(20), weight: '400', color: HA(0.3), key: 'hud.foot'
    });
    // 场景交界黑场「呼吸」（4s / 8s），避免硬切（wash 主题淡到自己的底色）
    var d = Math.min(Math.abs(t - 4.0), Math.abs(t - 8.0));
    var dip = clamp(1 - d / 0.3);
    if (dip > 0.001) g.flash(0.5 * E.out2(dip), C.wipe);
    // ↑ 这条在 dip 的"起坡帧"会有一个可见的亮度跳变（clamp 从 0 开始斜升）。
    //   对观众来说它是转场的一部分，但对逐帧质检它长得像单帧闪烁：
    //   所以把起坡时刻登记成 flash 事件，qa_frames 会把它从 flicker 里排除（见 FLICKER_EXPLAIN_WIN）。
  });
})(window);
