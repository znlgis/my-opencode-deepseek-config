# -*- coding: utf-8 -*-
"""scenes_hybrid_template.py — 在**内置渲染器**之上做最小改动的正确写法（模板）。

什么时候用
----------
形态 1（按段拼接）里想让图文段"像素材"（垫绢纹、加纸底、统一色温），但不想重写
10 类构图。做法：把内置 assets/scenes.py 当模块加载，只 patch 一个内部函数。

三条铁律（都实测踩过，违反任意一条都会出诡异 bug）
--------------------------------------------------
1. **先留原件再打补丁**
   `_ORIG = _S._plate` 必须在 `_S._plate = mine` 之前。
   反例：在自己的实现里调 `_S._plate(...)` —— 打补丁后它指向自己，递归爆栈。

2. **返回的必须是副本（copy）**
   内置绘制器的写法是 `img = _plate(...)` 然后直接在 img 上 ImageDraw 画字——
   它假定拿到的是本次调用独有的图（内置实现确实返回 base.copy()）。
   如果你返回**缓存对象本身**，同一场景每帧都会把文字叠到上一帧的结果上：
   文字重影、画面依赖渲染历史。实测后果：VMAF 从 97 掉到 **62.9**，
   而且 --jobs 4 的分块顺序会让不同分块拿到不同累积状态，几乎无法通过看单帧发现。

3. **缓存键必须由内容决定，且与"选哪张素材"用同一个索引**
   反例：key 用 `idx % 2`、选画用 `idx % 3` —— 键相同但画不同，不同进程/不同
   渲染顺序会命中不同素材，参考帧与成片对不上。

验证（写完必跑）
--------------
同一帧画两次 + 跨顺序画，必须**逐字节一致**：

    python tools/scenes_template_selftest.py   # 模板自带这份校验

这个模板做什么
----------------
把一张公有领域画的**低频明暗**（大半径模糊后压到 ±0.10）作为背景板加光，
得到"同一张绢、同一种光"的观感，但不出现可辨认的山形/物件（不抢文字注意力）。
实测三版对比：贴原图 → 抢文字；只取高频 → 像墨渍；压平低频 → 可用（本模板）。
"""
import hashlib
import importlib.util
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # skills/video-studio
BUILTIN = os.path.join(ROOT, "assets", "scenes.py")
MEDIA = os.environ.get("VIDEO_STUDIO_HYBRID_MEDIA", "")   # 素材目录（可选）

# ---- 1) 把内置渲染器当模块加载（不改它一个字节） ----
_spec = importlib.util.spec_from_file_location("_builtin_scenes", BUILTIN)
_S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_S)

W, H = _S.W, _S.H
AMP = 26.0                 # 加光幅度（像素级）：最亮 +13、最暗 -13，不压文字对比度
TINT = (1.00, 0.90, 0.74)  # 暖绢色：让灰阶纹理读作旧绢而不是灰雾
_WASH = {}                 # 素材名 -> 归一化低频明暗场
_PLATE = {}                # (场景 id, s_bg) -> 合成后的板（**存原件，返回副本**）


def _load_wash(name):
    """读一张画，压成"只有明暗起伏"的低频场（float32, -1..1）。"""
    if not MEDIA:
        return None
    path = os.path.join(MEDIA, name)
    if not os.path.exists(path):
        return None
    im = Image.open(path).convert("L")
    w, h = im.size
    s = max(W / float(w), H / float(h))
    im = im.resize((max(W, int(w * s)), max(H, int(h * s))), Image.LANCZOS)
    nw, nh = im.size
    x = max(0, (nw - W) // 2)
    y = max(0, int((nh - H) * 0.42))
    im = im.crop((x, y, x + W, y + H))
    small = im.resize((max(2, W // 24), max(2, H // 24)), Image.BOX)
    im = small.resize((W, H), Image.BICUBIC)
    a = np.asarray(im, dtype=np.float32) / 255.0
    a = a - float(a.mean())
    return (a / (float(np.abs(a).max()) + 1e-6)).astype(np.float32)


def _pick_scene_id(scene):
    """缓存的键：必须由**内容**决定（同一段永远命中同一张图）。"""
    return str(scene.get("id") or scene.get("chapter") or "")


def _textured_plate(th, lang, scene, s_bg):
    key = (_pick_scene_id(scene), round(float(s_bg), 4))
    got = _PLATE.get(key)
    if got is not None:
        return got.copy()              # ★ 铁律 2：必须复制，绘制器会在返回值上画字
    base = _ORIG_PLATE(th, lang, scene, s_bg).convert("RGB")
    names = sorted(_WASH) or ["_none_"]
    if not MEDIA:
        _PLATE[key] = base
        return base.copy()
    # 选哪张：用同一个键派生（★ 铁律 3：不能和缓存键用不同模数）
    h = int(hashlib.sha1(key[0].encode("utf-8")).hexdigest()[:4], 16)
    name = names[h % len(names)]
    tex = _WASH.get(name)
    if tex is None:
        tex = _load_wash(name)
        _WASH[name] = tex
    if tex is None:
        _PLATE[key] = base
        return base.copy()
    arr = np.asarray(base, dtype=np.float32)
    add = (tex * AMP)[:, :, None] * np.array(TINT, dtype=np.float32)[None, None, :]
    img = Image.fromarray(np.clip(arr + add, 0, 255).astype(np.uint8), "RGB")
    _PLATE[key] = img
    while len(_PLATE) > 24:            # LRU：多主题/多片同进程时防无限增长
        _PLATE.pop(next(iter(_PLATE)))
    return img.copy()                   # ★ 铁律 2


# ---- 2) 预载素材（可选：MEDIA 没设就自动退化为内置外观） ----
if MEDIA and os.path.isdir(MEDIA):
    for _n in sorted(os.listdir(MEDIA)):
        if _n.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            _w = _load_wash(_n)
            if _w is not None:
                _WASH[_n] = _w

# ---- 3) 打补丁（★ 铁律 1：先留原件） ----
_ORIG_PLATE = _S._plate
_S._plate = _textured_plate


# ---- 4) 对外契约：照原样转发 ----
def draw_frame(scene, t_local, dur, t_global, script, frame):
    return _S.draw_frame(scene, t_local, dur, t_global, script, frame)


def cues(tl, script):
    """提示点钩子：不改造就原样转发（不要让音效和画面脱节）。"""
    return _S.cues(tl, script)
