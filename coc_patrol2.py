#!/usr/bin/env python3
"""CoC 采集原型 v2：进村状态机 + 模板匹配气泡 + 逐个点击并用资源数字验证。"""
import os
import re
import subprocess
import sys
import time

import cv2
import numpy as np

SERIAL = os.environ.get("ANDROID_SERIAL", "").strip()   # 必填：adb devices 里的序列号
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
    """取一帧。优先 minicap（~80ms），不可用时 screencap（~1200ms）。"""
    global _mc
    if _mc:
        try:
            img = _mc.grab()
            if img is not None and img.shape[0] > img.shape[1]:
                _mc.restart()          # 游戏已横屏但投影还是竖屏，重建一次
                time.sleep(0.6)
                img = _mc.grab()
            if img is not None and img.size:
                if SHOT_LOG and tag:
                    cv2.imwrite(f"{OUT}/{tag}.png", img)
                return img
        except Exception:
            _mc = None                # 流断了就降级，不重连
    p = f"{OUT}/{tag or '_cur'}.png"
    with open(p, "wb") as f:
        subprocess.run(["adb", "-s", SERIAL, "exec-out", "screencap", "-p"],
                       stdout=f, timeout=30)
    return cv2.imread(p)


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
WORKER_BOX = (1950, 780, 2075, 880)  # 只框「0/5」工人计数，右边「1/1」是宝石/圣水符，会带出假读数


def read_workers(img):
    """返回 (在用, 总数)，读不到返回 (None, None)。白字压深色底，先用 HSV 滤底再放大 OCR。"""
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
CONFIRM_BTN = (1534, 947)      # 确认弹窗绿色「确认」


def _list_has_price(im):
    for t in ocr_all(im, (2050, 300, 2220, 1000)):
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
        out.append({"name": name, "price": price, "y": py, "kind": _kind_at(im, py)})
    return out


def upgrade_from_list(max_price=None, kinds=("gold", "elixir"), dry=False):
    """打开建筑列表，挑一条能付得起的升掉。返回 (item, workers_before, workers_after) 或 None。"""
    res = read_res_vote(3)              # 必须先读：列表会遮住右上资源栏
    for _ in range(3):
        if res.get("gold") and res.get("elixir"):
            break
        time.sleep(0.9)
        res = read_res_vote(3)
    w0 = read_workers(grab())
    im = open_build_list()
    if im is None:
        log("  打开建筑列表失败")
        return None
    items = read_build_list(im)
    if not items:
        log("  列表没读到可升级项")
        clear_panels()
        return None
    budget = {"gold": res.get("gold") or 0, "elixir": res.get("elixir") or 0,
              "dark": res.get("dark") or 0}
    pool = [i for i in items if i["kind"] in kinds]
    pool.sort(key=lambda i: i["price"])
    for it in pool:
        if max_price and it["price"] > max_price:
            continue
        if it["price"] > budget.get(it["kind"], 0) * 0.9:
            continue
        log(f"  候选 {it['name']} {it['price']:,} ({it['kind']})")
        if dry:
            clear_panels()
            return it, w0, w0
        cf, head = None, ""
        for _try in range(4):
            tap(LIST_ROW_X, it["y"], 1.0)
            cf = grab()
            if cf is None:
                continue
            head = " ".join(t["t"] for t in ocr_all(cf, (400, 20, 1800, 130)))
            if "升至" in head:
                break
        if "升至" not in head:
            log("  没弹确认框，跳过")
            clear_panels()
            continue
        tap(*CONFIRM_BTN, 2.0)
        time.sleep(1.5)
        clear_panels()
        res1 = read_res_vote(3)
        spent = budget.get(it["kind"], 0) - (res1.get(it["kind"]) or 0)
        if spent >= it["price"] * 0.9:
            log(f"  升级成功 {it['name']} 扣 {it['kind']} {spent:,}")
            return it, w0, read_workers(grab())
        log(f"  未见扣款（{it['kind']} {spent:,}/{it['price']:,}），可能未生效")
        return None
    log("  没有付得起的项目")
    clear_panels()
    return None

