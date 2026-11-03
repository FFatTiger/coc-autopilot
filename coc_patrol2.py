#!/usr/bin/env python3
"""CoC 采集原型 v2：进村状态机 + 模板匹配气泡 + 逐个点击并用资源数字验证。"""
import os
import re
import subprocess
import sys
import time

import cv2
import numpy as np

SERIAL = os.environ.get("ANDROID_SERIAL", "").strip()
PKG = "com.tencent.tmgp.supercell.clashofclans"
ACT = "com.supercell.titan.tencent.GameAppTencent"
OUT = "/tmp/probe"
TPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coc_templates")
os.makedirs(OUT, exist_ok=True)
os.makedirs(TPL, exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SHOT_LOG = os.environ.get("COC_SHOT_LOG") == "1"   # 设 1 才把每帧写盘（很慢，仅调试）


def sh(cmd, timeout=25):
    return subprocess.run(["adb", "-s", SERIAL, "shell", cmd],
                          capture_output=True, text=True, timeout=timeout)


_mc = None
_mc_tried = False


def init_capture():
    """启动 minicap 常驻流（快 10 倍）；拿不到就退回 screencap。"""
    global _mc, _mc_tried
    if _mc_tried:
        return _mc is not None
    _mc_tried = True
    try:
        from mc import Minicap
        _mc = Minicap(quality=90)
        _mc.grab()                      # 预热，确认能拿到帧
        return True
    except Exception as e:
        print(f"[capture] minicap 不可用，回退 screencap: {e}", flush=True)
        _mc = None
        return False


def close_capture():
    global _mc
    if _mc:
        try:
            _mc.close()
        except Exception:
            pass
        _mc = None


def grab(tag=None):
    """取一帧。优先 minicap（~80ms），失败先重连，连续失败才降级 screencap（~1200ms）。"""
    global _mc, _mc_fail
    if _mc is None and _mc_fail < 3:
        try:
            from mc import Minicap
            _mc = Minicap(connect=True)
            _mc_fail = 0
        except Exception:
            _mc = None
            _mc_fail += 1
    if _mc is not None:
        for attempt in range(2):
            try:
                img = _mc.grab()
                if img is not None and img.shape[0] > img.shape[1]:
                    _mc.restart()          # 横屏了但投影还是竖屏，重建一次
                    time.sleep(0.6)
                    img = _mc.grab()
                if img is not None and img.size:
                    _mc_fail = 0
                    if SHOT_LOG and tag:
                        cv2.imwrite(f"{OUT}/{tag}.png", img)
                    return img
                raise RuntimeError("空帧")
            except Exception:
                if attempt == 0:
                    try:
                        _mc.restart()
                        time.sleep(0.8)
                        continue
                    except Exception:
                        pass
                try:
                    _mc.close()
                except Exception:
                    pass
                _mc = None
                _mc_fail += 1
                break
    p = f"{OUT}/{tag or '_cur'}.png"
    with open(p, "wb") as f:
        subprocess.run(["adb", "-s", SERIAL, "exec-out", "screencap", "-p"],
                       stdout=f, timeout=30)
    img = cv2.imread(p)
    return img if img is not None and img.size else None


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


from collections import Counter

from rapidocr_onnxruntime import RapidOCR
_ocr = RapidOCR()


def ocr_all(img, region=None):
    x1, y1, x2, y2 = region or (0, 0, img.shape[1], img.shape[0])
    res, _ = _ocr(img[y1:y2, x1:x2])
    out = []
    for box, t, c in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({"t": t, "x": int(sum(xs) / 4) + x1, "y": int(sum(ys) / 4) + y1, "c": float(c)})
    return out


# ---------------- 资源读数 ----------------
RES_BANDS = {"gold": (30, 95), "elixir": (100, 175), "dark": (180, 260)}
RES_CROP = (1830, 2160)  # 数字区 x 范围（排除宝石图标）
RES_RANGE = {"gold": (1000, 20_000_000), "elixir": (1000, 20_000_000), "dark": (0, 500_000)}


def read_res(img):
    """读右上角金币/圣水/暗黑。

    OCR 会把 "6,065,002" 拆成 '6'/'065'/'002'，需要按 x 拼接；
    数字右对齐，所以从最右一段往左接，间距过大就断开（防止拼进邻行数字）。
    """
    x1, x2 = RES_CROP
    out = {"gold": None, "elixir": None, "dark": None}
    for k, (y1, y2) in RES_BANDS.items():
        roi = img[max(0, y1 - 6):y2 + 6, x1:x2]
        # 数字是白字压彩色底：白字低饱和高亮，底色高饱和，用 HSV 把底色滤掉
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = (((hsv[:, :, 2] > 150) & (hsv[:, :, 1] < 75)).astype(np.uint8)) * 255
        big = cv2.resize(mask, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
        toks = []
        for t in ocr_all(big):
            if t["c"] < 0.45:          # 低置信度多半是描边糊了，宁可丢掉也不拼错
                continue
            s = "".join(c for c in t["t"] if c.isdigit())
            if s:
                toks.append((t["x"], s))
        if not toks:
            continue
        toks.sort()
        keep = [toks[-1]]
        for x, s in reversed(toks[:-1]):
            if keep[0][0] - x > 520:
                break
            keep.insert(0, (x, s))
        j = "".join(s for _, s in keep)
        v = int(j) if len(j) >= 3 else None
        lo, hi = RES_RANGE[k]
        out[k] = v if (v is not None and lo <= v <= hi) else None
    return out


def read_res_until(n=3, tries=4, gap=1.2):
    """连拍投票，直到三项都读出来；用于有动画干扰的时机（收集后、升级后）。"""
    best = None
    for _ in range(tries):
        r = read_res_vote(n)
        if best is None:
            best = r
        for k in ("gold", "elixir", "dark"):
            if best.get(k) is None and r.get(k) is not None:
                best[k] = r[k]
        if all(best.get(k) is not None for k in ("gold", "elixir")):
            return best
        time.sleep(gap)
    return best


def read_res_vote(n=3):
    """连拍 n 帧分别读数，逐项投票。数字滚动时单帧 OCR 会认错，多帧多数票能纠回来。"""
    rows = [read_res(grab()) for _ in range(n)]
    out = {}
    for k in ("gold", "elixir", "dark"):
        vals = [r[k] for r in rows if r and r.get(k) is not None]
        out[k] = Counter(vals).most_common(1)[0][0] if vals else None
    return out


def read_res_stable(prev=None, tag="_res", tries=3):
    """读资源。和 prev 相比任一键骤降 >20% 就多读几帧取最大，避免 OCR 丢位。"""
    best = read_res(grab(tag))
    if not prev:
        return best
    for i in range(tries):
        bad = [k for k in ("gold", "elixir", "dark")
               if prev.get(k) and (best.get(k) is None or best[k] < prev[k] * 0.8)]
        if not bad:
            break
        alt = read_res(grab(f"{tag}{i}"))
        for k in bad:
            if alt.get(k) and (best.get(k) is None or alt[k] > best[k]):
                best[k] = alt[k]
    return best


# ---------------- 气泡检测 ----------------
def load_tpl(name):
    p = f"{TPL}/{name}.png"
    return cv2.imread(p) if os.path.exists(p) else None


def icon_kind(hsv, x, y):
    """按气泡中心图标的颜色判断资源类型（金币/圣水/暗黑）。"""
    y1, y2 = max(0, y - 16), min(hsv.shape[0], y + 16)
    x1, x2 = max(0, x - 16), min(hsv.shape[1], x + 16)
    sub = hsv[y1:y2, x1:x2]
    h = sub[:, :, 0].astype(float)
    s = sub[:, :, 1].astype(float)
    v = sub[:, :, 2].astype(float)
    m = s > 110
    if m.sum() < 60:
        return None
    H, S, V = float(np.median(h[m])), float(np.median(s[m])), float(np.median(v[m]))
    if V < 110:
        return "dark"
    if 135 <= H <= 175 and V > 140:
        return "elixir"
    if 10 <= H <= 42 and V > 150:
        return "coin"
    return None


def _match_tpl(gray, tpl, scales, thr):
    tg = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    hits = []
    for s in scales:
        t = cv2.resize(tg, None, fx=s, fy=s)
        if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1]:
            continue
        r = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(r >= thr)
        for y, x in zip(ys, xs):
            hits.append((float(r[y, x]), int(x + t.shape[1] // 2), int(y + t.shape[0] // 2)))
    hits.sort(reverse=True)
    keep = []
    for sc, x, y in hits:
        if any(abs(x - a) < 30 and abs(y - b) < 30 for _, a, b in keep):
            continue
        keep.append((sc, x, y))
    return keep


def find_bubbles(img):
    """模板匹配找资源气泡，再用中心图标颜色分类型。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    out = []
    coin = load_tpl("coin")
    if coin is not None:
        for sc, x, y in _match_tpl(gray, coin, [0.85, 0.9, 0.95, 1.0, 1.05], 0.62):
            if icon_kind(hsv, x, y) == "coin":
                out.append(("coin", x, y, sc))
    purple = load_tpl("elixir")
    if purple is not None:
        for sc, x, y in _match_tpl(gray, purple, [0.9, 1.0, 1.1, 1.2], 0.62):
            k = icon_kind(hsv, x, y)
            if k in ("elixir", "dark"):
                out.append((k, x, y, sc))
    out.sort(key=lambda h: -h[3])
    dedup = []
    for kind, x, y, sc in out:
        if any(abs(x - px) < 28 and abs(y - py) < 28 for _, px, py, _ in dedup):
            continue
        dedup.append((kind, x, y, sc))
    return dedup


# ---------------- 进村 ----------------
LOADING = ("正在载入", "载入游戏", "正在加载")
PANEL_BTN = ("确定", "完成", "关闭", "跳过", "继续", "知道了", "领取", "好的")
POPUP_WORDS = ("欢迎回来", "村庄被袭击", "离开村庄的这段时间")
QUIT_WORDS = ("确定退出", "退出游戏", "退出部落")   # 只能点「取消」，绝点「确定」
AD_WORDS = ("去看看", "现已上线", "终极皮肤", "系列皮肤", "限时礼包")
AD_CLOSE = (1938, 120)          # 活动广告弹窗右上角 X


def classify(tx):
    texts = [t["t"] for t in tx]
    joined = " ".join(texts)
    if any(k in joined for k in LOADING):
        return "loading"
    if any(k in joined for k in AD_WORDS):
        return "ad"
    if any(k in joined for k in QUIT_WORDS):
        return "quit_confirm"
    if any(k in joined for k in POPUP_WORDS):
        return "popup"
    # 底栏同时出现「建造」「战斗」= 村庄（最稳的位置判据）
    bottom = [t["t"] for t in tx if t["y"] > 940]
    if any("建造" in t for t in bottom) or any("战斗" in t for t in bottom):
        for t in tx:
            if t["y"] < 100 and len("".join(c for c in t["t"] if c.isdigit())) >= 5:
                return "village"
    if any(k in joined for k in ("重新载入", "连接失败", "网络异常")):
        return "reconnect"
    if any(len(t.strip()) <= 4 and any(k in t for k in PANEL_BTN) for t in texts):
        return "panel"
    return "unknown"


def find_btn(tx, keys):
    """精确匹配优先；子串匹配限制长度差，防止「完成」命中「完成的升级」。"""
    exact, partial = [], []
    for t in tx:
        s = t["t"].strip()
        if not (1 <= len(s) <= 6):
            continue
        for rank, k in enumerate(keys):
            if k == s:
                exact.append((rank, -t["c"], t))
                break
            if k in s and len(s) - len(k) <= 1:
                partial.append((rank, -t["c"], t))
                break
    exact.sort(key=lambda c: (c[0], c[1]))
    if exact:
        return exact[0][2]
    partial.sort(key=lambda c: (c[0], c[1]))
    return partial[0][2] if partial else None


_mt = None
_mt_tried = False


def _mt_tried_build():
    global _mt, _mt_tried
    if _mt is not None:
        return True
    return False


def tap(x, y, wait=1.2, hold=80):
    """只用 adb input tap：坐标由系统换算，实测正确；事件约半数被游戏丢弃，故短间隔补一次。"""
    xi, yi = int(x), int(y)
    sh(f"input tap {xi} {yi}")
    if wait and wait >= 0.5:
        time.sleep(0.18)
        sh(f"input tap {xi} {yi}")
    if wait:
        time.sleep(wait)


def enter_village(max_steps=25, warmup=16):
    """warmup 秒内只等不 OCR（游戏加载阶段本来就没东西可点）。"""
    if warmup:
        time.sleep(warmup)
    stable = 0
    last_btn = None
    same_btn = 0
    for step in range(max_steps):
        img = grab("_enter")
        tx = ocr_all(img)
        joined = " ".join(t["t"] for t in tx)
        st = classify(tx)
        if st == "village":
            stable += 1
            if stable >= 2:
                cv2.imwrite(f"{OUT}/_enter_ok.png", img)
                log("进村完成")
                return img
            time.sleep(1.5)
            continue
        stable = 0
        if st in ("loading", "unknown"):
            time.sleep(2.5)
            continue
        if st == "ad":
            log("  关广告弹窗")
            tap(AD_CLOSE[0], AD_CLOSE[1], 1.2)
            continue
        if st == "quit_confirm":
            b = find_btn(tx, ("取消",))
            if b:
                log(f"  取消退出确认 @({b['x']},{b['y']})")
                tap(b["x"], b["y"], 1.5)
            else:
                tap(2147, 145, 1.0)
            continue
        if st == "popup":
            # 只有确认是「欢迎回来」才点确定；其他不明弹窗先点空白试探，绝不盲点确定
            if any(w in joined for w in POPUP_WORDS):
                b = find_btn(tx, ("确定",))
                if b:
                    log(f"  关欢迎弹窗 @({b['x']},{b['y']})")
                    tap(b["x"], b["y"], 1.5)
                    continue
            log("  不明弹窗，点空白试探")
            tap(1110, 300, 1.2)
            continue
        b = find_btn(tx, PANEL_BTN + ("载入", "重试"))
        if b:
            key = (b["t"], b["x"] // 40, b["y"] // 40)
            if key == last_btn:
                same_btn += 1
            else:
                same_btn = 0
                last_btn = key
            if same_btn >= 2:
                log(f"  {b['t']!r} 点不动，换点空白")
                tap(1110, 320, 1.2)
                last_btn = None
                same_btn = 0
                continue
            log(f"  关弹窗 {b['t']!r} @({b['x']},{b['y']})")
            tap(b["x"], b["y"], 1.5)
        elif st == "reconnect":
            tap(2147, 145, 1.0)
        else:
            time.sleep(2.0)
    return None


# ---------------- 视野归一化 ----------------
def zoom_to_min(cx=1110, cy=540, max_tries=14, tol=1.2):
    """双指捏合把视野缩到最小，让建筑坐标固定下来。返回捏合次数。

    缩放完成后两轮之间的尺度差约 0.57%、平移 <1px，坐标表才有意义。
    """
    from mt import Minitouch
    t = Minitouch()
    prev = None
    n = 0
    try:
        for i in range(max_tries):
            t.pinch(cx, cy, 640, 160, 550, 12)
            time.sleep(0.7)
            cur = grab()
            n = i + 1
            if prev is not None and cv2.absdiff(prev, cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)).mean() < tol:
                break
            prev = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
    finally:
        t.close()
    return n


# ---------------- 村庄状态检查 ----------------
STAR_BOX = (50, 798, 205, 866)      # 左下角 5 颗胜利之星
WORKER_BOX = (1950, 780, 2075, 880)  # 只框「空闲/总数」工人计数，右边「1/1」是宝石/圣水符


def read_workers(img):
    """返回 (空闲, 总数)，读不到返回 (None, None)。

    游戏右下角的「0/5」是「空闲工人/总工人」——0 表示全在忙，不是全空闲。
    """
    x1, y1, x2, y2 = WORKER_BOX
    roi = img[y1:y2, x1:x2]
    if roi.size:
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = (((hsv[:, :, 2] > 150) & (hsv[:, :, 1] < 80)).astype(np.uint8)) * 255
        big = cv2.resize(mask, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
        for t in ocr_all(big):
            s = t["t"].replace(" ", "")
            if "/" in s:
                a, b = s.split("/")[:2]
                if a.isdigit() and b.isdigit():
                    return int(a), int(b)
    for t in ocr_all(img, WORKER_BOX):
        s = t["t"].replace(" ", "")
        if "/" in s:
            a, b = s.split("/")[:2]
            if a.isdigit() and b.isdigit():
                return int(a), int(b)
    return None, None


def read_star(img):
    """每日胜利之星已完成数量 0-5：五颗星按亮度区分亮/暗。"""
    x1, y1, x2, y2 = STAR_BOX
    g = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    w = g.shape[1]
    lit = 0
    for i in range(5):
        seg = g[:, int(w * i / 5):int(w * (i + 1) / 5)]
        if float(np.percentile(seg, 95)) > 180:     # 亮星有近白像素，暗星最亮只到 ~100
            lit += 1
    return lit


# ---------------- 收集 ----------------
PANEL_WORDS = ("信息", "选择整行", "升级更多", "堡垒之城", "升级")
FORBIDDEN = ("购买", "点券", "宝石", "宝藏", "礼包", "充值")
DESELECT = ((300, 700), (205, 400))   # 两块空地，连点两次关面板更稳


def deselect():
    for x, y in DESELECT:
        tap(x, y, 0.3)


def panel_ratio(img, box=(450, 640, 1850, 1080)):
    if img is None or not getattr(img, "size", 0):
        return 0.0
    """面板木纹黄色占比。实测：无面板 0.04-0.06，面板打开 0.13+。"""
    x1, y1, x2, y2 = box
    hsv = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (13, 70, 130), (25, 255, 235))
    return (m > 0).sum() / max(m.size, 1)


def panel_open(img):
    """底部被面板遮住时，木纹黄色占比要么飙升（木纹面板）要么骤降（浅色面板）。

    实测：村庄 0.038-0.044，木纹面板 0.13+，浅色面板 0.001。
    """
    r = panel_ratio(img)
    return r > 0.09 or r < 0.02


def panel_state(img):
    """返回 (是否打开建筑面板, 该区域的 OCR 文本)。"""
    tx = ocr_all(img, (450, 640, 1850, 1080))
    texts = [t["t"] for t in tx]
    hit = bool(texts) and (panel_open(img) or any(any(w in t for w in PANEL_WORDS) for t in texts))
    return hit, tx


def clear_panels(max_try=2):
    """确保画面上没有残留面板。点空地关不掉就退回 BACK 键兜底。"""
    for _ in range(max_try):
        if not panel_open(grab("_pre")):
            return True
        deselect()
    img = grab("_pre")
    if not panel_open(img):
        return True
    sh("input keyevent 4")                     # BACK 一定能关面板
    time.sleep(0.5)
    img = grab("_pre")
    if any("确定退出" in t["t"] for t in ocr_all(img)):
        b = find_btn(ocr_all(img), ("取消",))
        if b:
            tap(b["x"], b["y"], 0.5)
    return not panel_open(grab("_pre"))


_last_state = None


TAP_OFFSETS = ((0, 0), (0, 60), (0, 95), (0, -45))   # 气泡可点区常常在正下方


def collect_one(x, y):
    """点气泡；画面没反应就微调位置重试。返回 (状态, 点击后截图)。"""
    global _last_state
    if _last_state in ("panel", "shop", "stuck") and not clear_panels():
        _last_state = "stuck"
        return "stuck", None
    base = grab("_base")
    g0 = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY) if base is not None else None
    last = None
    for dx, dy in TAP_OFFSETS:
        img = None
        for _retry in range(2):                 # minitouch 事件约有半数被游戏丢弃，同点补一次
            tap(x + dx, y + dy, 0.40)
            img = grab("_tap")
            if img is not None and panel_open(img):
                break
        if img is None:
            continue
        last = img
        if panel_open(img):
            is_panel, tx = panel_state(img)
            if is_panel and any(any(w in t["t"] for w in FORBIDDEN) for t in tx):
                deselect()
                _last_state = "shop"
                return "shop", img
            btn = find_btn([t for t in tx if t["y"] > 640], ("收集",))
            if btn:
                tap(btn["x"], btn["y"], 0.45)
                _last_state = "collect"
                return "collect", img
            deselect()
            _last_state = "panel"
            return "panel", img
        if g0 is not None and cv2.absdiff(g0, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)).mean() > 1.2:
            _last_state = "direct"
            return "direct", img
    _last_state = "noop"
    return "noop", last


def collect(img, dry=False):
    if img is not None:
        cv2.imwrite(f"{OUT}/_collect_start.png", img)
    before = read_res_vote()
    w = read_workers(img)
    star = read_star(img)
    log(f"村庄状态：工人 {w[0]}/{w[1]}  胜利之星 {star}/5")
    log(f"收取前 {before}")
    bubbles = find_bubbles(img)
    log(f"气泡 {len(bubbles)} 个: {[(k, x, y) for k, x, y, _ in bubbles]}")
    d = img.copy()
    for kind, x, y, sc in bubbles:
        col = {"coin": (0, 200, 255), "elixir": (255, 0, 255), "dark": (200, 150, 255)}[kind]
        cv2.circle(d, (x, y), 34, col, 3)
        cv2.putText(d, kind, (x - 20, y + 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    cv2.imwrite(f"{OUT}/marked.png", d)

    ok = 0
    prev = before
    global _last_state
    _last_state = None
    for i, (kind, x, y, sc) in enumerate(bubbles):
        if dry:
            continue
        state, shot = collect_one(x, y)
        gained = 0
        cur = prev
        if state in ("direct", "collect"):
            if shot is not None and panel_open(shot):
                deselect()
            cur = read_res_vote()
            for k in ("gold", "elixir", "dark"):
                if prev.get(k) is not None and cur.get(k) is not None:
                    gained += max(0, cur[k] - prev[k])
            prev = cur
        log(f"  [{i+1}/{len(bubbles)}] {kind} ({x},{y}) {state} Δ={gained} now={cur}")
        if state in ("direct", "collect") or gained > 0:
            ok += 1
    after = read_res_vote()
    clear_panels()                                  # 收尾别把面板留在画面上
    log(f"收取后 {after} | 成功 {ok}/{len(bubbles)}")
    return before, after, ok


if __name__ == "__main__":
    try:
        sh("input keyevent KEYCODE_WAKEUP")
        time.sleep(1.5)
        sh(f"am force-stop {PKG}")
        time.sleep(0.8)
        sh(f"am start -n {PKG}/{ACT}")
        init_capture()
        img = enter_village()
        if img is not None:
            collect(img)
    finally:
        close_capture()
        sh(f"am force-stop {PKG}")
        sh("input keyevent KEYCODE_SLEEP")
        log(sh("dumpsys power | grep -m1 mWakefulness=").stdout.strip())

# ---------- 建筑和英雄列表（自动升级入口） ----------
BUILD_BTN = (2091, 918)        # 右下角锤子「建造」
LIST_BOX = (1600, 50, 2220, 1010)
LIST_ROW_X = 1900              # 列表行点击 x
CONFIRM_BTN = (1497, 935)      # 确认弹窗绿色「确认」（文字 1497,906 / 价格 1467,959）


def _list_has_price(im):
    if im is None:
        return False
    for t in ocr_all(im, (1930, 300, 2220, 1010)):
        d = "".join(c for c in t["t"] if c.isdigit())
        if len(d) >= 4 and not any(u in t["t"] for u in ("天", "小时", "分钟")):
            return True
    return False


def open_build_list():
    """点锤子打开「建筑和英雄」列表，并确保展开到显示价格；失败返回 None。"""
    base = grab()
    if base is None:
        return None
    b = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    im = None
    for _ in range(6):
        tap(*BUILD_BTN, 0.45)
        im = grab()
        if im is not None and cv2.absdiff(b, cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)).mean() > 8:
            break
    if im is None:
        return None
    if not _list_has_price(im):       # 列表收窄时价格不可见，拖右边缘展开
        sh(f"input swipe 1883 37 2120 37 500")
        time.sleep(1.0)
        wide = grab()
        if wide is not None:
            im = wide
    return im


def _kind_at(im, py):
    """按价格右侧图标的颜色判定资源类型：黄=金币、紫=圣水、暗=暗黑重油。"""
    patch = im[max(0, int(py) - 10):int(py) + 10, 2170:2208]
    if patch.size == 0:
        return "?"
    px = patch.reshape(-1, 3)
    hsv = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    bright = hsv[(hsv[:, 1] > 120) & (hsv[:, 2] > 140)]
    if len(bright) >= 15:
        h = float(np.median(bright[:, 0]))
        if 15 <= h <= 45:
            return "gold"
        if 120 <= h <= 170:
            return "elixir"
    if float(np.median(hsv[:, 2])) < 130 and float(np.median(hsv[:, 1])) < 130:
        return "dark"
    return "?"


def read_build_list(im):
    """解析列表条目 [{name, price, y, kind}]：以右对齐的价格数字为锚点，配同行的名字。"""
    tx = ocr_all(im, (1680, 50, 2220, 1010))
    prices, names = [], []
    for t in tx:
        d = "".join(c for c in t["t"] if c.isdigit())
        if any(u in t["t"] for u in ("天", "小时", "分钟")):
            continue                        # 倒计时不是价格
        if t["x"] >= 2048 and len(d) >= 3:
            prices.append((int(d), t["x"], t["y"]))
        elif t["x"] < 2048 and len(t["t"]) >= 2 and not t["t"].replace("×", "").isdigit():
            names.append(t)
    out, used = [], set()
    for price, px, py in prices:
        cand = [n for n in names if abs(n["y"] - py) < 40 and id(n) not in used]
        if not cand:
            continue
        n = min(cand, key=lambda z: abs(z["y"] - py))
        used.add(id(n))
        name = n["t"].strip()
        if price < 1000:                    # 读错的价格（0、三位数）直接丢弃
            continue
        out.append({"name": name, "price": price, "y": py, "kind": _kind_at(im, py)})
    return out


def find_green_button(img, box=(900, 850, 1900, 1040)):
    """在确认弹窗底部找最大的绿色按钮，返回其中心。绿色是「确认」，灰色是「取消」，别点错。"""
    if img is None or not img.size:
        return None
    x1, y1, x2, y2 = box
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    b, g, r = roi[:, :, 0].astype(int), roi[:, :, 1].astype(int), roi[:, :, 2].astype(int)
    # CoC 的确认按钮是黄绿：实测 rgb≈(210,244,137)，G 最高、B 最低
    mask = ((g > 200) & (g - b > 60) & (r > 140) & (r < 252)).astype(np.uint8) * 255
    if int(mask.sum()) // 255 < 1500:      # 绿像素太少，多半不在确认框
        return None
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[idx, cv2.CC_STAT_AREA] < 3000:
        return None
    # 连通域含按钮阴影和高光，几何中心偏右下；实测可点区在 bbox 左上 18% 处
    left = int(stats[idx, cv2.CC_STAT_LEFT])
    top = int(stats[idx, cv2.CC_STAT_TOP])
    w = int(stats[idx, cv2.CC_STAT_WIDTH])
    h = int(stats[idx, cv2.CC_STAT_HEIGHT])
    cx = left + int(w * 0.32)          # 水平 32%
    cy = top + int(h * 0.18)           # 垂直 18%（实测 y≈900 有效，bbox 879~995）
    return int(cx + x1), int(cy + y1), int(stats[idx, cv2.CC_STAT_AREA])



def _village_ready():
    """回到能读数的村庄状态。"""
    for _ in range(3):
        if not panel_open(grab()):
            r = read_res_vote(2)
            if r.get("gold") is not None and r.get("elixir") is not None:
                return r
        clear_panels()
        time.sleep(1.2)
    return None


def _read_items_retry(max_try=3):
    """打开列表并读到条目，读不到就拖动展开重来。"""
    for _ in range(max_try):
        im = open_build_list()
        if im is None:
            clear_panels()
            continue
        items = read_build_list(im)
        if items and all(i["kind"] != "?" for i in items[:2]):
            return im, items
        log(f"  列表不完整（{len(items)} 项），拖动展开重试")
        sh("input swipe 1883 37 2120 37 500")
        time.sleep(1.2)
        im2 = grab()
        if im2 is not None:
            items = read_build_list(im2)
            if items and all(i["kind"] != "?" for i in items[:2]):
                return im2, items
        clear_panels()
        time.sleep(1.0)
    return None, []


def upgrade_from_list(max_price=None, kinds=("gold", "elixir"), dry=False, items=None, res=None, w0=None):
    """挑一条能付得起的升掉。整条链路带状态验证，任一步不对就回村重来。"""
    for attempt in range(2):
        res0 = res if (res and res.get("gold")) else _village_ready()
        if not res0:
            log("  回不到村庄状态，放弃本轮升级")
            return None
        w_start = w0 or read_workers(grab())

        if items:
            im, lst = None, items
            items = None
        else:
            im, lst = _read_items_retry()
        if not lst:
            log("  读不到建筑列表")
            return None

        budget = {"gold": res0.get("gold") or 0, "elixir": res0.get("elixir") or 0,
                  "dark": res0.get("dark") or 0}
        pool = [i for i in lst if i["kind"] in kinds]
        pool.sort(key=lambda i: i["price"])
        log(f"  预算 g={budget['gold']:,} e={budget['elixir']:,} d={budget['dark']:,} | "
            f"候选 {[(i['name'], i['price'], i['kind']) for i in pool[:3]]}")

        picked = None
        for it in pool:
            if max_price and it["price"] > max_price:
                continue
            if it["price"] > budget.get(it["kind"], 0) * 0.9:
                continue
            picked = it
            break
        if picked is None:
            log("  没有付得起的项目")
            clear_panels()
            return None
        if dry:
            clear_panels()
            return picked, w_start, w_start

        # 点行 → 等确认框；列表行点击常被丢，最多试 6 次
        cf, ok = None, False
        for _t in range(6):
            tap(LIST_ROW_X, picked["y"], 0.9)
            cf = grab()
            if cf is None:
                continue
            head = " ".join(t["t"] for t in ocr_all(cf) if t["y"] < 140)
            if "升至" in head:
                ok = True
                break
        if not ok:
            log("  没弹出确认框，回村重来")
            clear_panels()
            time.sleep(1.0)
            continue

        # 点确认：用绿色像素定位，找不到退回固定坐标；点到弹窗消失为止
        btn = find_green_button(cf)
        pos = (btn[0], btn[1]) if btn else CONFIRM_BTN
        log(f"  确认框已弹出，按钮 @{pos}")
        for _c in range(3):
            tap(pos[0], pos[1], 1.3)
            time.sleep(1.0)
            chk = grab()
            if chk is None:
                continue
            if not any("升至" in t["t"] for t in ocr_all(chk) if t["y"] < 140):
                break

        # 验证：回村后看扣款或工人数
        time.sleep(2.0)
        res1 = read_res_until(3, tries=4, gap=1.2)
        clear_panels()
        time.sleep(1.2)
        if res1.get("gold") is None:
            res1 = read_res_until(3, tries=3, gap=1.0)
        w1 = read_workers(grab())
        cur = res1.get(picked["kind"])
        spent = (budget.get(picked["kind"], 0) - cur) if cur is not None else None
        paid = (picked["price"] > 0 and spent is not None
                and spent >= picked["price"] * 0.9)
        busy = (w_start and w1 and w_start[0] is not None and w1[0] is not None
                and w1[0] > w_start[0])
        if paid or busy:
            why = f"扣 {picked['kind']} {spent:,}" if paid else f"工人 {w_start[0]}->{w1[0]}"
            log(f"  升级成功 {picked['name']}（{why}）")
            return picked, w_start, w1
        log(f"  未确认生效（spent={spent} 工人{w_start}->{w1}），重试")
        clear_panels()
        time.sleep(1.2)
    log("  两次尝试都没成功")
    return None
