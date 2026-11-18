/* engine.js — Canvas2D 逐帧确定性动画内核（v0.8.0）
 *
 * 改编自 ai-history-film（MIT）video/web/js/engine.js 的「逐帧 seek + 场景 + cue」思路，
 * 但去掉 GSAP 与 three.js，改成原生 Canvas2D + 自写缓动，不 vendor 任何三方 JS。
 *
 * v0.8.0 新增：风格注入（window.__STYLE__）。webengine.open_page(style=...) 在页面加载前用
 * add_init_script 写入它，本文件据此设置画布 w/h、帧率、字体栈与底色；**没有 __STYLE__ 时
 * 所有取值退回 v0.7 常量，像素与旧版逐字节一致**（style=None 的兼容性硬要求）。
 *
 * 核心约定：
 * - 每帧从零重画：画面只由「帧号 f」决定，同一帧号永远画同一张图（逐帧渲染可复现）。
 * - scene(id, start, dur, build)：场景 = 时间窗口 + 若干逐帧绘制回调，窗口外不执行。
 * - 不碰 Math.random / Date.now / performance.now；随机数一律 hash() / rng(seed)。
 * - 文字只从 g.text() 画，顺带登记包围盒与透明度，window.qaText 用它做文字质检。
 * - cue()/event() 登记声音提示点与视觉事件，供 window.getCues()/getEvents() 导出。
 */
(function (global) {
  'use strict';

  /* ---------------- v0.8.0：风格包（window.__STYLE__） ----------------
   * 由 webengine 注入（页面自己直接打开时没有）。不注入 = 全部退回下面这行常量。
   * 只读 payload，不在页面里做任何派生计算；同 payload 永远渲染出同一张图。 */
  var ST = (global.__STYLE__ && typeof global.__STYLE__ === 'object') ? global.__STYLE__ : null;
  var ST_THEME = (ST && ST.theme && typeof ST.theme === 'object') ? ST.theme : null;
  var ST_CANVAS = (ST && ST.canvas && typeof ST.canvas === 'object') ? ST.canvas : null;

  /* 页面在 engine.js 之前自己声明过的帧率（模板不声明 → null）。页面契约说
   * "window.FPS = 页面时间轴帧率，渲染器据此算 -r / -g"，所以声明过就不能被默认 30 顶掉
   * —— 否则 webengine 的"页面 FPS 与注入 fps 不一致就报错"永远看不到真实页面帧率。 */
  var DECLARED_FPS = _num(global.FPS, null);
  var W = 1920, H = 1080, BG = '#0b0e14';
  var FPS = (DECLARED_FPS != null && DECLARED_FPS > 0) ? Math.round(DECLARED_FPS) : 30;
  var DEFAULT_DURATION = 12.0;   // 页面没给时长时的兜底；film.js 用它区分"显式时长"与"兜底值"
  var FAMILY = '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif';
  var TAU = Math.PI * 2;

  function _num(v, fb) { var n = Number(v); return (v != null && isFinite(n)) ? n : fb; }
  function _txt(v, fb) { return (typeof v === 'string' && v.length > 0) ? v : fb; }

  var canvas = document.getElementById('stage');
  if (!canvas) throw new Error('engine.js: 页面缺少 <canvas id="stage">');

  /* 画布尺寸：只在注入 style 时改（webengine 侧已校验 16:9 横屏 / 9:16 竖屏偶数，这里再兜一层）。
   * 没注入时 canvas 属性 / 内联样式一个字都不动 —— 旧行为不变。
   * 这里的比例白名单必须与 webengine._validate_canvas 一致：上游放行 1080x1920 而这里只认
   * 16:9 时，竖屏片会在页面里抛错（浏览器侧一句"必须是 16:9 横屏"），比上游拒绝更难查。 */
  if (ST_CANVAS) {
    var _w = Math.round(_num(ST_CANVAS.w, W)), _h = Math.round(_num(ST_CANVAS.h, H));
    var _r = (_w > 0 && _h > 0) ? _w / _h : 0;
    var _okAspect = _r === 0
      || Math.abs(_r - 16 / 9) <= 1e-3 || Math.abs(_r - 9 / 16) <= 1e-3;
    if (!_okAspect) {
      throw new Error('engine.js: __STYLE__.canvas 必须是 16:9 横屏或 9:16 竖屏，收到 '
        + _w + 'x' + _h);
    }
    if (_w > 0 && _h > 0) {
      W = _w; H = _h;
      canvas.width = W;
      canvas.height = H;
      canvas.style.width = W + 'px';
      canvas.style.height = H + 'px';
      document.documentElement.style.width = W + 'px';
      document.documentElement.style.height = H + 'px';
      if (document.body) {
        document.body.style.width = W + 'px';
        document.body.style.height = H + 'px';
      }
    }
    /* 帧率：只有调用方显式给了 fps 才改。帧率是 timeline 契约，不是画风 ——
     * 不能因为注入了 style 就悄悄把页面帧率换掉（那样分块帧号会整体错位）。
     * 页面自己声明过 window.FPS 且与注入值不一致时**不覆盖**：保留页面值，让
     * webengine.open_page 的契约检查报出"页面 window.FPS=X 与注入的 fps=Y 不一致"。
     * 以前这里无条件覆盖 → 页面 window.FPS 被改成注入值，那条检查永远相等，
     * 文档承诺的"fps 不一致就报错"对 engine.js 页面从未真正触发过。 */
    var _fps = _num(ST_CANVAS.fps, null);
    var _declared = (DECLARED_FPS != null && DECLARED_FPS > 0) ? Math.round(DECLARED_FPS) : null;
    if (_fps != null && _fps > 0) {
      if (_declared != null && _declared !== Math.round(_fps)) {
        // 保留页面声明的帧率（FPS 已在上面按 DECLARED_FPS 初始化），交给 webengine 报错
      } else {
        FPS = Math.round(_fps);
      }
    }
  }

  if (ST_THEME) BG = _txt(ST_THEME.bg, BG);
  if (ST && ST.typography) FAMILY = _txt(ST.typography.font_stack, FAMILY);

  var ctx = canvas.getContext('2d', { alpha: false, desynchronized: false });

  /* ---------------------------------------------------------------- 数学 / 缓动 */
  function clamp(x, a, b) {
    if (a === undefined) a = 0;
    if (b === undefined) b = 1;
    return x < a ? a : (x > b ? b : x);
  }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function seg(t, a, b) { return clamp((t - a) / ((b - a) || 1e-9)); }
  function round3(v) { return Math.round(v * 1000) / 1000; }

  // 自写缓动表：名字对齐常见动画库的语义，但实现只有几行，不引入任何依赖。
  var E = {
    lin: function (x) { return x; },
    in2: function (x) { return x * x; },
    out2: function (x) { return 1 - (1 - x) * (1 - x); },
    inOut2: function (x) { return x < 0.5 ? 2 * x * x : 1 - 2 * (1 - x) * (1 - x); },
    in3: function (x) { return x * x * x; },
    out3: function (x) { return 1 - Math.pow(1 - x, 3); },
    inOut3: function (x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; },
    out5: function (x) { return 1 - Math.pow(1 - x, 5); },
    outExpo: function (x) { return x >= 1 ? 1 : 1 - Math.pow(2, -10 * x); },
    inExpo: function (x) { return x <= 0 ? 0 : Math.pow(2, 10 * x - 10); },
    inOutExpo: function (x) {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      return x < 0.5 ? Math.pow(2, 20 * x - 10) / 2 : (2 - Math.pow(2, -20 * x + 10)) / 2;
    },
    outBack: function (x) { var c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2); }
  };

  // 确定性伪随机：mulberry32 + FNV 风格 hash（同 ai-history-film 的 rng/hash，纯整数运算）
  function rng(seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function hash() {
    var h = 2166136261;
    for (var i = 0; i < arguments.length; i++) {
      h ^= (arguments[i] * 2654435761) >>> 0;
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0) / 4294967296;
  }

  /* ---------------------------------------------------------------- 颜色 */
  function rgb(c) {
    if (Array.isArray(c)) return c;
    var s = String(c == null ? '#ffffff' : c).trim();
    if (s.charAt(0) === '#') {
      if (s.length === 4) return [parseInt(s.charAt(1) + s.charAt(1), 16), parseInt(s.charAt(2) + s.charAt(2), 16), parseInt(s.charAt(3) + s.charAt(3), 16)];
      return [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];
    }
    var m = s.match(/rgba?\(([^)]+)\)/);
    if (m) { var p = m[1].split(','); return [parseInt(p[0], 10) | 0, parseInt(p[1], 10) | 0, parseInt(p[2], 10) | 0]; }
    return [255, 255, 255];
  }
  function rgba(c, a) { var v = rgb(c); return 'rgba(' + v[0] + ',' + v[1] + ',' + v[2] + ',' + a + ')'; }

  /* ---------------------------------------------------------------- 发光精灵（离屏缓存，避免逐粒子建渐变） */
  var spriteCache = new Map();
  function glowSprite(color) {
    var key = rgb(color).join(',');
    var got = spriteCache.get(key);
    if (got) return got;
    var cv = document.createElement('canvas');
    cv.width = 128; cv.height = 128;
    var g2 = cv.getContext('2d');
    var grd = g2.createRadialGradient(64, 64, 0, 64, 64, 64);
    grd.addColorStop(0, 'rgba(' + key + ',1)');
    grd.addColorStop(0.35, 'rgba(' + key + ',0.55)');
    grd.addColorStop(1, 'rgba(' + key + ',0)');
    g2.fillStyle = grd;
    g2.fillRect(0, 0, 128, 128);
    spriteCache.set(key, cv);
    return cv;
  }

  /* ---------------------------------------------------------------- 文字登记（qaText 的数据源） */
  var frameTexts = new Map();   // 当前帧：key -> {key,group,text,x0,y0,x1,y1,alpha}
  var drawnKeys = new Set();    // 本次质检扫描期间真正露面过的 key / group
  var watchers = new Map();     // key -> 中文标签（页面声明"这段文字应该出现"）

  function watch(key, label) { watchers.set(String(key), String(label || key)); }
  function qaReset() { drawnKeys.clear(); }
  function watched() {
    var out = [];
    watchers.forEach(function (label, key) { out.push({ key: key, label: label }); });
    return out;
  }
  function drawnKeyList() { return Array.from(drawnKeys); }
  function texts() {
    var out = [];
    frameTexts.forEach(function (q) {
      out.push({ key: q.key, group: q.group, text: q.text, x0: q.x0, y0: q.y0, x1: q.x1, y1: q.y1, alpha: q.alpha });
    });
    return out;
  }
  function recordText(key, group, str, box, alpha) {
    var cur = frameTexts.get(key);
    if (!cur) {
      frameTexts.set(key, { key: key, group: group || '', text: str, x0: box.x0, y0: box.y0, x1: box.x1, y1: box.y1, alpha: alpha });
    } else {
      cur.text = str;
      cur.alpha = Math.max(cur.alpha, alpha);
      cur.x0 = Math.min(cur.x0, box.x0); cur.y0 = Math.min(cur.y0, box.y0);
      cur.x1 = Math.max(cur.x1, box.x1); cur.y1 = Math.max(cur.y1, box.y1);
      if (group) cur.group = group;
    }
    if (alpha >= 0.3) {
      drawnKeys.add(key);
      if (group) drawnKeys.add(group);
    }
  }

  /* ---------------------------------------------------------------- 绘图原语 */
  function text(str, x, y, o) {
    o = o || {};
    str = (str == null) ? '' : String(str);
    if (!str) return;
    var size = o.size || 48;
    var weight = o.weight || '600';
    var color = o.color || '#ffffff';
    var align = o.align || 'left';
    var base = o.baseline || 'middle';
    var alpha = (o.alpha == null) ? 1 : o.alpha;
    var key = o.key || ('t:' + str);
    var group = o.group || '';
    ctx.save();
    ctx.font = weight + ' ' + size + 'px ' + (o.family || FAMILY);
    ctx.textAlign = align;
    ctx.textBaseline = base;
    var m = ctx.measureText(str);
    var w = m.width;
    var asc = (typeof m.actualBoundingBoxAscent === 'number') ? m.actualBoundingBoxAscent : size * 0.8;
    var desc = (typeof m.actualBoundingBoxDescent === 'number') ? m.actualBoundingBoxDescent : size * 0.2;
    var fAsc = (typeof m.fontBoundingBoxAscent === 'number') ? m.fontBoundingBoxAscent : size * 0.9;
    var fDesc = (typeof m.fontBoundingBoxDescent === 'number') ? m.fontBoundingBoxDescent : size * 0.25;
    var off;
    if (base === 'top') off = fAsc;
    else if (base === 'bottom') off = -fDesc;
    else if (base === 'middle') off = (fAsc - fDesc) / 2;
    else off = 0;
    var x0 = align === 'center' ? x - w / 2 : (align === 'right' ? x - w : x);
    recordText(key, group, str, { x0: x0, y0: y + off - asc, x1: x0 + w, y1: y + off + desc }, alpha);
    if (alpha > 0.002) {
      ctx.globalAlpha = alpha;
      ctx.fillStyle = color;
      ctx.fillText(str, x, y);
    }
    ctx.restore();
  }

  function measure(str, size, weight, family) {
    ctx.save();
    ctx.font = (weight || '600') + ' ' + (size || 48) + 'px ' + (family || FAMILY);
    var w = ctx.measureText(String(str)).width;
    ctx.restore();
    return w;
  }

  // 逐字排版：返回每个字的相对 x 与宽度，供"逐字入场"用
  function layoutChars(str, size, weight, family, tracking) {
    tracking = tracking || 0;
    var chars = Array.from(String(str));
    var widths = chars.map(function (c) { return measure(c === ' ' ? '\u00a0' : c, size, weight, family); });
    var total = 0, i;
    for (i = 0; i < widths.length; i++) total += widths[i];
    total += tracking * Math.max(0, chars.length - 1);
    var out = [];
    var x = 0;
    for (i = 0; i < chars.length; i++) {
      out.push({ ch: chars[i], x: x, w: widths[i] });
      x += widths[i] + tracking;
    }
    return { chars: out, width: total };
  }

  function rect(x, y, w, h, fill, o) {
    o = o || {};
    if (!(w > 0) || !(h > 0)) return;
    ctx.save();
    ctx.globalAlpha = (o.alpha == null) ? 1 : o.alpha;
    ctx.fillStyle = fill;
    ctx.fillRect(x, y, w, h);
    ctx.restore();
  }

  function rrect(x, y, w, h, r, fill, o) {
    o = o || {};
    if (!(w > 0) || !(h > 0)) return;
    ctx.save();
    ctx.globalAlpha = (o.alpha == null) ? 1 : o.alpha;
    ctx.fillStyle = fill;
    ctx.beginPath();
    if (typeof ctx.roundRect === 'function') {
      ctx.roundRect(x, y, w, h, r);
    } else {
      var rr = Math.min(r, Math.min(w, h) / 2);
      ctx.moveTo(x + rr, y);
      ctx.arcTo(x + w, y, x + w, y + h, rr);
      ctx.arcTo(x + w, y + h, x, y + h, rr);
      ctx.arcTo(x, y + h, x, y, rr);
      ctx.arcTo(x, y, x + w, y, rr);
      ctx.closePath();
    }
    ctx.fill();
    ctx.restore();
  }

  function line(x0, y0, x1, y1, color, width, o) {
    o = o || {};
    ctx.save();
    ctx.globalAlpha = (o.alpha == null) ? 1 : o.alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 2;
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    ctx.restore();
  }

  function circle(x, y, r, fill, o) {
    o = o || {};
    if (!(r > 0)) return;
    ctx.save();
    ctx.globalAlpha = (o.alpha == null) ? 1 : o.alpha;
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, TAU);
    ctx.fill();
    ctx.restore();
  }

  function ring(x, y, r, color, width, o) {
    o = o || {};
    if (!(r > 0)) return;
    ctx.save();
    ctx.globalAlpha = (o.alpha == null) ? 1 : o.alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 2;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, TAU);
    ctx.stroke();
    ctx.restore();
  }

  function glow(x, y, r, color, alpha) {
    if (!(r > 0) || !(alpha > 0.001)) return;
    ctx.save();
    ctx.globalAlpha = Math.min(1, alpha);
    ctx.globalCompositeOperation = 'lighter';
    ctx.drawImage(glowSprite(color), x - r, y - r, r * 2, r * 2);
    ctx.restore();
  }

  function flash(alpha, color) {
    if (!(alpha > 0.001)) return;
    ctx.save();
    ctx.globalAlpha = Math.min(1, alpha);
    ctx.fillStyle = color || '#000000';
    ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }

  function bgGradient(stops) {
    var grd = ctx.createLinearGradient(0, 0, 0, H);
    for (var i = 0; i < stops.length; i++) {
      grd.addColorStop(clamp(stops[i][0]), rgba(stops[i][1], 1));
    }
    ctx.save();
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }

  function grid(o) {
    o = o || {};
    var step = o.step || 120;
    var color = o.color || [90, 210, 255];
    var t = o.t || 0;
    var drift = o.drift || 0;
    var off = ((t * drift) % step + step) % step;
    ctx.save();
    ctx.strokeStyle = rgba(color, (o.alpha == null) ? 0.06 : o.alpha);
    ctx.lineWidth = 1;
    ctx.beginPath();
    var x, y;
    for (x = -off; x <= W; x += step) { ctx.moveTo(x, 0); ctx.lineTo(x, H); }
    for (y = 0; y <= H; y += step) { ctx.moveTo(0, y); ctx.lineTo(W, y); }
    ctx.stroke();
    ctx.restore();
  }

  function vignette(strength) {
    var s = (strength == null) ? 0.55 : strength;
    if (!(s > 0)) return;
    var grd = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.32, W / 2, H / 2, Math.max(W, H) * 0.72);
    grd.addColorStop(0, 'rgba(0,0,0,0)');
    grd.addColorStop(1, 'rgba(0,0,0,' + s + ')');
    ctx.save();
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }

  // 粒子：位置 = hash(seed,i,*) + 时间 → 同一 t 永远同一张图，与播放历史无关
  function particles(o) {
    o = o || {};
    var n = o.n || 120;
    var seed = (o.seed == null) ? 7 : o.seed;
    var t = o.t || 0;
    var area = o.area || { x: 0, y: 0, w: W, h: H };
    var color = o.color || [255, 140, 66];
    var base = (o.alpha == null) ? 0.55 : o.alpha;
    var speed = (o.speed == null) ? 1 : o.speed;
    var rmin = (o.rmin == null) ? 2 : o.rmin;
    var rmax = (o.rmax == null) ? 6 : o.rmax;
    var wobble = (o.wobble == null) ? 26 : o.wobble;
    for (var i = 0; i < n; i++) {
      var a = hash(seed, i, 1);
      var b = hash(seed, i, 2);
      var c = hash(seed, i, 3);
      var px = ((a + t * speed * (0.006 + 0.02 * c)) % 1 + 1) % 1;
      var x = area.x + px * area.w;
      var y = area.y + b * area.h + Math.sin(t * (0.4 + c * 1.1) * speed + a * TAU) * wobble;
      var r = (rmin + (rmax - rmin) * c) * (0.7 + 0.35 * Math.sin(t * (1.2 + a) + b * TAU));
      glow(x, y, Math.max(1.5, r * 3.2), color, base * (0.35 + 0.65 * c));
    }
  }

  var g = {
    text: text, measure: measure, layoutChars: layoutChars,
    rect: rect, rrect: rrect, line: line, circle: circle, ring: ring,
    glow: glow, flash: flash, bgGradient: bgGradient, grid: grid, vignette: vignette, particles: particles,
    rgb: rgb, rgba: rgba
  };

  /* ---------------------------------------------------------------- 场景 / 时间轴 */
  var scenes = [];
  var updaters = [];
  var cueList = [];
  var eventList = [];

  function scene(id, start, dur, build) {
    var S = {
      id: id,
      start: start,
      dur: dur,
      update: function (fn) {
        updaters.push({ a: start, b: start + dur, start: start, fn: fn });
        return S;
      },
      cue: function (t, type, params) {
        var o = { t: round3(start + (Number(t) || 0)), type: type, src: 'web' };
        if (params) { for (var k in params) { if (Object.prototype.hasOwnProperty.call(params, k)) o[k] = params[k]; } }
        cueList.push(o);
        return S;
      },
      event: function (t, kind, params) {
        var o = { t: round3(start + (Number(t) || 0)), kind: kind };
        if (params) { for (var k in params) { if (Object.prototype.hasOwnProperty.call(params, k)) o[k] = params[k]; } }
        eventList.push(o);
        return S;
      }
    };
    if (typeof build === 'function') build(S);
    scenes.push(S);
    return S;
  }

  function always(fn) { updaters.push({ a: -1e9, b: 1e9, start: 0, fn: fn }); }

  function copySorted(list) {
    return list.slice().sort(function (a, b) { return a.t - b.t; }).map(function (c) {
      var o = {};
      for (var k in c) { if (Object.prototype.hasOwnProperty.call(c, k)) o[k] = c[k]; }
      return o;
    });
  }
  function getCues() { return copySorted(cueList); }
  function getEvents() { return copySorted(eventList); }

  /* 场景表推出的时间轴总长（最后一个场景的 end）。
   * 为什么要它：页面契约里的 DURATION 必须等于真正的成片秒数，而 v0.3 的模板在 film.js
   * 开头写死 global.DURATION = 12.0 —— 加一幕/改一幕时长后忘了同步这个常量，就会
   * "明明改了场景、成片还是 12 秒"或者直接和 timeline.json 对不上（render.py 报
   * 页面 DURATION 与 timeline 总时长不一致）。让 DURATION 从场景表推，就不会漏改。 */
  function timelineEnd() {
    var end = 0;
    for (var i = 0; i < scenes.length; i++) {
      var e = Number(scenes[i].start) + Number(scenes[i].dur);
      if (isFinite(e) && e > end) end = e;
    }
    return Math.round(end * 1000) / 1000;
  }

  /* ---------------------------------------------------------------- 逐帧渲染 */
  var currentFrame = -1;

  function renderFrame(f) {
    f = Math.max(0, Math.round(Number(f) || 0));
    currentFrame = f;
    frameTexts = new Map();
    var T = f / FPS;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, W, H);
    for (var i = 0; i < updaters.length; i++) {
      var u = updaters[i];
      // 半开窗口 [a, b)：相邻场景在边界帧不重复绘制
      if (T + 1e-9 >= u.a && T < u.b - 1e-9) {
        ctx.save();
        try {
          u.fn(T - u.start, T, f);
        } finally {
          ctx.restore();
        }
      }
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    return new Promise(function (res) {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(function () { res(true); });
      else setTimeout(function () { res(true); }, 0);
    });
  }

  /* ---------------------------------------------------------------- 启动 */
  var booted = false;
  async function boot() {
    if (booted) return true;
    try {
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
    } catch (e) {
      // 字体 API 不存在也不阻塞渲染（系统字体兜底）
    }
    // 字形预热：让首帧前完成字体度量，避免 FOUT 造成的前几帧字宽抖动
    var warm = '逐帧确定性Canvas极简时间轴数字粒子发光渲染0123456789·→ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    ctx.save();
    ctx.font = '700 148px ' + FAMILY;
    ctx.measureText(warm);
    ctx.restore();
    await renderFrame(0);
    booted = true;
    global.ready = true;
    return true;
  }

  /* ---------------------------------------------------------------- 导出 */
  var FK = {
    W: W, H: H, FPS: FPS, BG: BG, TAU: TAU, FAMILY: FAMILY,
    style: ST,                      // 注入的 __STYLE__（没有就是 null）；qa / selftest 可据此断言
    defaultDuration: DEFAULT_DURATION,
    canvas: canvas, ctx: ctx,
    clamp: clamp, lerp: lerp, seg: seg, E: E, hash: hash, rng: rng,
    g: g,
    scene: scene, always: always, watch: watch, qaReset: qaReset,
    texts: texts, watched: watched, drawnKeys: drawnKeyList,
    timelineEnd: timelineEnd,
    getCues: getCues, getEvents: getEvents, renderFrame: renderFrame, boot: boot,
    frame: function () { return currentFrame; }
  };
  global.FK = FK;
  global.FPS = FPS;
  global.renderFrame = renderFrame;
  global.getCues = getCues;
  global.getEvents = getEvents;
  if (typeof global.DURATION === 'undefined') global.DURATION = DEFAULT_DURATION;
  global.ready = false;
})(window);
