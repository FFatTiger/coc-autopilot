#!/usr/bin/env python3
"""从游戏截图里裁图标 → static/icons/*.png（透明底，96×96，页面 base64 内嵌）。

为什么这么做：页面上资源卡片、在建列表、历史状态都要图标，但项目定死了「零外部资源」，
不能用 CDN、不能用 emoji（游戏页面配 emoji 很出戏）。所以直接从项目里已有的真截图裁：
  · 资源图标（金/圣水/暗黑/宝石）从资源条和建筑面板的米色卡片上裁；
  · 锤子直接用 coc_templates/panel_hammer.png（本来就是从「升级」按钮上裁的模板）。
底都是纯色/单色背景，按饱和度 / 亮度把背景抠掉，留 alpha。裁不到的不硬编，跳过即可——
页面那边有兜底（按类型着色的圆角块 + 首字母）。

用法：
  python3 tools/icons_build.py                 # 用默认源图（logs/ + history/ 里的真截图）
  python3 tools/icons_build.py --check         # 只打印裁到的图标尺寸，不写文件
"""
import argparse
import glob
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "static", "icons")
SIZE = 96          # 显示 24~34px，96 够 2x~3x 屏


def _png_bytes(bgra, colors=128):
    """BGRA → 尽量小的 PNG（先调色板量化，保持透明；单图标目标 < 4KB）"""
    try:
        from PIL import Image
        im = Image.fromarray(cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA), "RGBA")
        q = im.quantize(colors=colors, method=Image.FASTOCTREE)
        import io
        buf = io.BytesIO()
        q.save(buf, format="PNG", optimize=True)
        if len(buf.getvalue()) < 4000:
            return buf.getvalue()
    except Exception:
        pass
    ok, buf = cv2.imencode(".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    return buf.tobytes() if ok else b""


def _finish(rgb, alpha, size=SIZE):
    """rgb(BGR) + alpha(0..255) → 预乘后缩放 → BGRA（避免缩小时边缘发黑）"""
    a = (alpha.astype(np.float32) / 255.0)[..., None]
    pre = rgb.astype(np.float32) * a
    pre = cv2.resize(pre, (size, size), interpolation=cv2.INTER_AREA)
    a2 = cv2.resize(a, (size, size), interpolation=cv2.INTER_AREA).reshape(size, size, 1)
    out = np.dstack([pre / np.maximum(a2, 1e-3), np.clip(a2 * 255, 0, 255)])
    return np.clip(out, 0, 255).astype(np.uint8)


def _square(im, cx=None, cy=None, side=None):
    h, w = im.shape[:2]
    side = side or min(h, w)
    cx = w // 2 if cx is None else cx
    cy = h // 2 if cy is None else cy
    half = side // 2
    x0, y0 = max(0, cx - half), max(0, cy - half)
    return im[y0:y0 + side, x0:x0 + side]


def _bbox(mask, pad=3):
    ys, xs = np.where(mask > 0)
    if not len(xs):
        return None
    return (max(0, xs.min() - pad), max(0, ys.min() - pad),
            xs.max() + pad + 1, ys.max() + pad + 1)


def _clean(mask, close=3, grow=1):
    m = (mask > 0).astype(np.uint8) * 255
    if close:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close)))
    if grow:
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow * 2 + 1,) * 2))
    return cv2.GaussianBlur(m, (5, 5), 1.2)


# ---------------------------------------------------------------- 抠图配方
def cut_bright_on_light(im, s_min=110, v_max=135, pad=4):
    """米色卡片上的彩色图标（金币/宝石）：彩色(s 高) 或 压暗的描边(v 低) 都留下"""
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    S, V = hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    mask = ((S > s_min) | (V < v_max)).astype(np.uint8) * 255
    bb = _bbox(mask, pad)
    if not bb:
        return None
    x0, y0, x1, y1 = bb
    return _finish(im[y0:y1, x0:x1], _clean(mask[y0:y1, x0:x1]))


def cut_dark_on_light(im, v_max=170, pad=4):
    """米色卡片上的暗色图标（暗黑重油）：只看亮度"""
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    V = hsv[:, :, 2].astype(int)
    mask = (V < v_max).astype(np.uint8) * 255
    bb = _bbox(mask, pad)
    if not bb:
        return None
    x0, y0, x1, y1 = bb
    return _finish(im[y0:y1, x0:x1], _clean(mask[y0:y1, x0:x1]))


def cut_convex(im, pred, pad=3, shrink=0):
    """按颜色选出图标 → 取最大连通块 → 凸包当遮罩（把背景碎片挡在外面）。

    为什么用凸包：金币/宝石这种外轮廓是凸的（圆、六边形、水滴），而背景里跟图标同色
    的碎片（草地、进度条）总在轮廓外侧，凸包一取就干净了。
    """
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0].astype(int), hsv[:, :, 1].astype(int), hsv[:, :, 2].astype(int)
    mask = pred(H, S, V).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(st[1:, 4]))
    comp = (lab == i).astype(np.uint8) * 255
    pts = cv2.findNonZero(comp)
    if pts is None or len(pts) < 20:
        return None
    hull = cv2.convexHull(pts)
    m = np.zeros_like(comp)
    cv2.fillPoly(m, [hull], 255)
    if shrink:
        m = cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (shrink * 2 + 1,) * 2))
    else:
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    bb = _bbox(m, pad)
    if not bb:
        return None
    x0, y0, x1, y1 = bb
    return _finish(im[y0:y1, x0:x1], cv2.GaussianBlur(m[y0:y1, x0:x1], (5, 5), 1.0))


def cut_by_bg(im, thresh=34, pad=2):
    """背景是单色卡片时最省事：跟边框中位色差得远的就是图标（锤子这种灰白图标用它）"""
    k = 4
    border = np.concatenate([im[:k].reshape(-1, 3), im[-k:].reshape(-1, 3),
                             im[:, :k].reshape(-1, 3), im[:, -k:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    d = np.linalg.norm(im.astype(np.float32) - bg[None, None, :], axis=2)
    mask = (d > thresh).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    bb = _bbox(mask, pad)
    if not bb:
        return None
    x0, y0, x1, y1 = bb
    return _finish(im[y0:y1, x0:x1], cv2.GaussianBlur(mask[y0:y1, x0:x1], (5, 5), 1.0))


# ---------------------------------------------------------------- 源图
def find_one(*pats):
    for p in pats:
        hits = sorted(glob.glob(os.path.join(ROOT, p)))
        if hits:
            return hits[-1]
    return None


def load(p):
    return cv2.imread(p) if p and os.path.isfile(p) else None


RECIPES = [
    # 名字, 源图候选, 裁剪框 (x0,y0,x1,y1) 或 None, 抠图函数
    ("gold", ["logs/*_panel_Gold_Mine.jpg", "logs/*panel_Gold*.jpg"],
     (1330, 760, 1450, 870), cut_bright_on_light),
    ("elixir", ["history/*/before.jpg"], (2110, 78, 2210, 178), None),   # 见 build()
    ("dark", ["logs/*_panel_Dark_Elixir_Drill.jpg"], (1330, 760, 1450, 870), cut_dark_on_light),
    ("gems", ["history/*/before.jpg"], (2125, 230, 2215, 330), None),
]


def build(check=False):
    os.makedirs(OUT, exist_ok=True)
    n = 0

    def emit(name, bgra):
        nonlocal n
        if bgra is None:
            print("  跳过", name)
            return
        p = os.path.join(OUT, name + ".png")
        raw = b""
        if not check:
            raw = _png_bytes(bgra)
            with open(p, "wb") as fh:
                fh.write(raw)
        n += 1
        print("  %-12s %s %d bytes" % (name, bgra.shape[:2], len(raw)))

    # 金：面板米色卡上的大金币
    im = load(find_one(*RECIPES[0][1]))
    if im is not None:
        x0, y0, x1, y1 = RECIPES[0][2]
        emit("gold", RECIPES[0][3](im[y0:y1, x0:x1]))

    # 圣水：确认页绿按钮上的水滴（洋红 vs 绿色按钮，色相差得远，最好抠）
    im = load(find_one("logs/*_confirm_*.jpg", "history/*/before.jpg"))
    if im is not None:
        emit("elixir", cut_convex(im[870:1010, 1330:1670],
                                  lambda H, S, V: (H > 135) & (H < 175) & (S > 110) & (V > 90),
                                  shrink=3))

    # 暗黑：面板米色卡上的黑水滴
    im = load(find_one(*RECIPES[2][1]))
    if im is not None:
        x0, y0, x1, y1 = RECIPES[2][2]
        emit("dark", RECIPES[2][3](im[y0:y1, x0:x1]))

    # 宝石：资源条上的绿宝石（六边形，凸包正好把它从树丛里摘出来）
    im = load(find_one(*RECIPES[3][1]))
    if im is not None:
        emit("gems", cut_convex(im[230:330, 2100:2215],
                                lambda H, S, V: (H > 30) & (H < 70) & (S > 110) & (V > 120)))


    print("共 %d 个图标 → %s" % (n, OUT))
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    build(check=a.check)
