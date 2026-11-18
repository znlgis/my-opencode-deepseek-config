/* qa_text.js — 浏览器端文字质检（v0.8.0）
 *
 * 逐帧重渲染画布，读回 engine.js 登记的文字包围盒，找出四类问题：
 *   ① overlaps   文字互相压住（同一 group 内的逐字排版不算）
 *   ② offscreen  出画（包围盒越过画布边界 6px；边界取 FK.W/FK.H，v0.8 起画布可非 1080p）
 *   ③ brief      一闪而过（可见时长 < 0.6s；按采样点数×步长算，不是首末跨度）
 *   ④ never      从没露面（需页面用 FK.watch(key, 标签) 声明）
 *
 * 深审修复：参数 fail-closed —— step 非正 / >= 0.6s（brief 阈值）或 t1<t0 直接抛错。
 * 以前这三种情况会返回空结果，调用方（CLI textqa）把它当"零缺陷"exit 0：门禁看着是绿的，
 * 其实一行都没查。
 *
 * 判据改编自 ai-history-film（MIT）video/qa_text.js：
 * 重叠面积比 0.08、出画容差 6px、短命阈值 0.6s。
 */
(function (global) {
  'use strict';

  function r2(v) { return Math.round(v * 100) / 100; }
  function short(s) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return s.length > 18 ? s.slice(0, 18) + '…' : s;
  }
  function nameOf(q) { return (q.group || q.key) + '「' + short(q.text) + '」'; }

  /* 「一闪而过」判据：可见时长 < 0.6 s（与 webengine.QA_BRIEF_S / 文档同口径）。
   * 采样步长必须小于它，否则每段文字最多被采到 1 个点，该判据永远不可能触发 —— 那时
   * 返回一个"零缺陷"的结果等于骗人，所以这里直接抛错（Python 侧也有一道同样的校验，
   * 为的是 CLI 能给 exit 2 + 可读报错）。 */
  var BRIEF_S = 0.6;

  async function qaText(t0, t1, step) {
    var F = global.FK;
    if (!F) throw new Error('qa_text.js: 找不到 window.FK（engine.js 未加载？）');
    var start = (t0 == null) ? 0 : Number(t0);
    var dur = Number(global.DURATION) || 0;
    var end = (t1 == null || !isFinite(Number(t1))) ? dur : Number(t1);
    var fps = Number(global.FPS) || 30;
    var stepN = (step == null) ? 0.2 : Number(step);
    if (!isFinite(start) || !isFinite(end)) {
      throw new Error('qaText: 时间范围必须是有限秒数（t0=' + t0 + ' t1=' + t1 + '）');
    }
    if (!isFinite(stepN) || stepN <= 0) {
      throw new Error('qaText: step 必须是正数秒（收到 ' + step + '）');
    }
    if (stepN >= BRIEF_S) {
      throw new Error('qaText: step=' + stepN + 's 必须小于 ' + BRIEF_S
        + 's（一闪而过阈值）；步长 >= 阈值时每段文字最多被采到 1 个点，该判据永远不可能触发');
    }
    if (end < start) {
      throw new Error('qaText: t1=' + end + ' < t0=' + start
        + '（空扫描不是"没有缺陷"，不能当通过）');
    }
    var dt = Math.max(stepN, 1 / fps);
    F.qaReset();
    var sampled = 0;    // 实际扫描的采样帧数（=== frames 字段，与文档一致）

    var life = new Map();    // key -> {text, group, first, last, n}
    var overl = new Map();   // 名字对 -> {at, to, ratio}
    var off = new Map();     // 名字   -> {at, to, box}
    var visTotal = 0;

    for (var T = start; T <= end + 1e-6; T += dt) {
      sampled += 1;
      await global.renderFrame(Math.round(T * fps));
      var all = F.texts().filter(function (q) {
        return q.alpha >= 0.3 && (q.x1 - q.x0) >= 2 && (q.y1 - q.y0) >= 2;
      });
      visTotal += all.length;

      for (var i = 0; i < all.length; i++) {
        var q = all[i];
        var L = life.get(q.key);
        if (!L) { L = { text: q.text, group: q.group, first: T, last: T, n: 0 }; life.set(q.key, L); }
        L.last = T;
        L.n += 1;
        if (q.x0 < -6 || q.x1 > F.W + 6 || q.y0 < -6 || q.y1 > F.H + 6) {
          var k = nameOf(q);
          var o = off.get(k);
          if (!o) {
            o = { at: T, to: T, box: [Math.round(q.x0), Math.round(q.y0), Math.round(q.x1), Math.round(q.y1)], el: k };
            off.set(k, o);
          }
          o.to = T;
        }
      }

      for (var a = 0; a < all.length; a++) {
        for (var b = a + 1; b < all.length; b++) {
          var A = all[a];
          var B = all[b];
          if (A.key === B.key) continue;
          if (A.group && A.group === B.group) continue;
          var w = Math.min(A.x1, B.x1) - Math.max(A.x0, B.x0);
          var h = Math.min(A.y1, B.y1) - Math.max(A.y0, B.y0);
          if (w <= 2 || h <= 2) continue;
          var ar = (w * h) / Math.min((A.x1 - A.x0) * (A.y1 - A.y0), (B.x1 - B.x0) * (B.y1 - B.y0));
          if (ar < 0.08) continue;
          var pk = nameOf(A) + ' × ' + nameOf(B);
          var po = overl.get(pk);
          if (!po) { po = { at: T, to: T, ratio: 0 }; overl.set(pk, po); }
          po.to = T;
          if (ar > po.ratio) po.ratio = ar;
        }
      }
    }

    var brief = [];
    life.forEach(function (L, key) {
      /* 可见时长 = 被采到的点数 × 步长。不能再用 last - first + dt（首末跨度）：
       * 同一个 key 如果在两段互不相连的时间各闪 0.1 s（比如 t=0 与 t=5.9 各一次），
       * 跨度会把 0.2 s 的闪烁算成 6.1 s 的可见时长而漏报。连续出现时两种算法等价
       * （点数 = 跨度/dt + 1），所以换成 n*dt 只影响"分散出现"这一类，判据更严格也
       * 更符合文档写的"可见时长"。 */
      var d = L.n * dt;
      if (d < 0.6) brief.push({ at: r2(L.first), dur: r2(d), el: (L.group || key) + '「' + short(L.text) + '」' });
    });

    var drawn = new Set(F.drawnKeys());
    var never = F.watched()
      .filter(function (w) { return !drawn.has(w.key); })
      .map(function (w) { return w.label; });

    var overlaps = [];
    overl.forEach(function (o, k) { overlaps.push({ at: r2(o.at), to: r2(o.to), ratio: r2(o.ratio), pair: k }); });
    var offscreen = [];
    off.forEach(function (o, k) { offscreen.push({ at: r2(o.at), to: r2(o.to), box: o.box, el: k }); });

    overlaps.sort(function (x, y) { return x.at - y.at; });
    offscreen.sort(function (x, y) { return x.at - y.at; });
    brief.sort(function (x, y) { return x.at - y.at; });
    // frames = 采样帧数（12 s / step 0.2 → 61，与 references/web-engine.md 的示例一致）；
    // texts  = 扫描期间累计可见文本实例数（原来的 visTotal，语义本来就不同）。
    return { overlaps: overlaps, offscreen: offscreen, brief: brief, never: never,
             frames: sampled, texts: visTotal };
  }

  global.qaText = qaText;
})(window);
