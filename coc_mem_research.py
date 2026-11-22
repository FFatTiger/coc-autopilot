#!/usr/bin/env python3
"""实验室研究（兵种 / 法术）：点实验室 → 面板「研究」→ 逐页读研究对象 → 挑一个 → 确认页核对 → 确认 → 验扣费。

为什么要单独一层
  升级建筑是「工人干活」，研究是「实验室干活」，两件事的界面、约束、验证方式都不一样：
  建筑升级有 5 个工位、面板里直接带价格；研究只有 1 个槽位、要进弹窗一页页翻卡片。
  混在一个函数里，任何一处改动都会牵连另一处，所以分开。

界面（19.100.1 实测，2220x1080，UI 坐标不随镜头变）
  实验室面板：5 张卡（信息 / 指派助手 / 提速 / 升级 / 研究）。「研究」是最右那张，但底栏
  卡片数会随高亮卡（蓝底那张不参与米色检测）变化，所以按 OCR 认「研究」两个字定卡。
  研究弹窗：标题「选择要升级的对象」，6 列 × 2 行 = 一屏 12 个对象（兵种 / 法术 / 攻城机器
  混排，圣水与暗黑重油分开标价），左右各有 ◀ ▶ 箭头，**横向滑动**翻页（竖着滑会把弹窗关掉）。
  每格：左下深色方块是当前等级，下面一行是价格 + 币种图标；灰格写「需要11级实验室」。
  点格子直接进「将<N>升至<L>级？」确认页：绿色「确认」按钮上印价格 + 币种图标，
  左侧灰按钮是「战斗之锤」（花魔法物品，绝对不点）。右上角红 X 退出不扣费。

安全闸（任一处不过就不点确认）
  ① 确认页必须认出来（绿色按钮 + 中部浅灰卡片占比），认不到就 X 退出
  ② 标题必须解析出「对象名 + 目标等级」，否则说明点错格子
  ③ 绿色按钮上的价格读得出、币种在允许列表、不超该币种单次上限、库存够
  ④ 扣费以内存差值为准；和面板价对不上记 alert（不静默）
  宝石绝不参与：任何「立即完成 / 加速」按钮都不点，只点绿色「确认」。

用法
  python3 coc_mem_research.py --dry     # 只读不点：打印研究对象清单（等级/价格/币种/是否可研究）
  python3 coc_mem_research.py           # 真跑：有槽位且资源够就研究一个（花了就花了）
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import coc_backend_mem as mem
import coc_mem_buildings as bldlib
import coc_mem_panel as panel

STATE = os.path.join(HERE, "coc_auto_state.json")
CACHE = os.path.join(HERE, "research_catalog.json")   # 价格|等级 → 名字/币种（点过一次就记住）
LOGD = os.path.join(HERE, "logs")
LAB_TMPL = os.path.join(HERE, "coc_templates", "lab_locator.png")   # 实验室贴图（中心=点击点）
LAB_TMPL_META = os.path.join(HERE, "coc_templates", "lab_locator.json")

# ---- 研究弹窗几何（19.100.1 实测；UI 固定坐标） ----
POP_X0, POP_X1 = 428, 1799          # 弹窗左右边界
GRID_Y0, GRID_Y1 = 505, 1005        # 卡片区上下边界（OCR 只扫这一带）
ROW_Y = (712, 940)                  # 两行卡片「价格那一行」的 y 中心
X_CLOSE = (1755, 64)                # 研究弹窗右上角红 X（实测 bbox x1716-1793 y27-101）
X_CLOSE_CONFIRM = (1846, 85)         # 研究确认页右上角红 X（bbox x1808-1885 y50-120）
X_CLOSE_OLD = (1837, 70)             # 旧值：留在村庄里正好落在金币栏上的那个红点，别再用了
SW_L, SW_R = (1500, 700, 600, 700, 250), (600, 700, 1500, 700, 250)   # 翻页左/右
MAX_PAGES = 8
LABEL_Y = 884                       # 底栏卡片图标/文字的点击 y
LABEL_BAND = (885, 992)             # 底栏卡片标签的 OCR 带（含描边，裁太紧 OCR 会把「研究」读成「咻九」）
LABEL_BAND_ALT = (890, 985)          # 备用带：两条带取认得多的那条

DIGITS_RE = re.compile(r"^\d[\d\s,]*$")
TITLE_RE = re.compile(r"将(.+?)升至\s*(\d+)\s*级")
TIME_RE = re.compile(r"(?:(\d+)\s*天)?\s*(?:(\d+)\s*小时)?")


def iso_now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def jload(p, d=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return d


def jsave(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, p)


def ocr_boxes(im, region):
    """在 region 里 OCR → [{x, y, x0, x1, y0, y1, t, c}]，坐标都已回推到整图。

    要 bbox 不只是中心点：币种图标紧贴在价格数字右边，靠 bbox 的右边界才能精确切到图标，
    用中心点 + 固定偏移会在 7 位数价格上落到数字上（实测把 2,500,000 圣水读成金币）。
    """
    import coc_patrol2
    x0, y0, x1, y1 = region
    res, _ = coc_patrol2._ocr(im[y0:y1, x0:x1])
    out = []
    for box, t, c in (res or []):
        txt = (t or "").strip()
        if float(c or 0) < 0.45 or not txt:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append(dict(x=int(sum(xs) / 4) + x0, y=int(sum(ys) / 4) + y0,
                        x0=int(min(xs)) + x0, x1=int(max(xs)) + x0,
                        y0=int(min(ys)) + y0, y1=int(max(ys)) + y0,
                        t=txt, c=float(c)))
    return out


def _has_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _num(s):
    """'2 500 000' / '2,500,000' / '25-500' → int；不认识返回 None"""
    s = s.replace(" ", "").replace(",", "").replace("-", "")
    return int(s) if s.isdigit() else None


def _sig(im):
    """一屏卡片的粗指纹：翻页到头时画面不变，用它判断"""
    return hashlib.md5(cv2.resize(im[GRID_Y0:GRID_Y1, POP_X0:POP_X1],
                                  (200, 120)).tobytes()).hexdigest()


# ---------------------------------------------------------------- 一屏读数
def _white_boxes(im, region, v=200, s_max=90, scale=2.5):
    """白色像素掩码 + 放大后再 OCR：卡片底色是彩色的（药水、兵种立绘），直接 OCR 会把
    5 读成 S、把前导数字吃掉（实测 page2 的 5,000,000 读成 S000000、2,000,000 读成 0000002）。
    只留白字（价格就是白字压深色描边）就干净了。"""
    x0, y0, x1, y1 = region
    sub = im[y0:y1, x0:x1]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    m = (((hsv[:, :, 2] > v) & (hsv[:, :, 1] < s_max)).astype(np.uint8)) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    big = cv2.resize(m, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    big = cv2.cvtColor(big, cv2.COLOR_GRAY2BGR)
    out = ocr_boxes(big, (0, 0, big.shape[1], big.shape[0]))
    for b in out:
        b["x"] = int(b["x"] / scale) + x0
        b["y"] = int(b["y"] / scale) + y0
        b["bbox"] = (int(b["x0"] / scale) + x0, int(b["x1"] / scale) + x0,
                     int(b["y0"] / scale) + y0, int(b["y1"] / scale) + y0)
    return out


def icon_kind_win(im, x0, x1, y0, y1):
    """窗口里的币种图标 → 'gold'/'elixir'/'dark'/None（按三类像素票数判）"""
    sub = im[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(int)
    sat = hsv[:, :, 1].astype(int)
    v = hsv[:, :, 2].astype(int)
    # 金币图标色相实测 H≈26；上界卡到 35 是为了不把确认按钮的柠檬绿（H 40~55）算成金币
    gold = int(((h >= 12) & (h <= 35) & (sat > 110) & (v > 140)).sum())
    elx = int(((h >= 130) & (h <= 172) & (sat > 110) & (v > 140)).sum())
    dark = int(((h >= 110) & (h <= 175) & (sat > 50) & (v >= 30) & (v < 130)).sum())
    if dark >= 450 and dark > elx * 1.5:
        return "dark"
    if elx >= 300 and elx >= gold:
        return "elixir"
    if gold >= 400 and gold > elx * 1.5:
        return "gold"
    return None


def icon_kind(im, cx, cy):
    """价格右边图标的币种 → 'gold'/'elixir'/'dark'/None（研究格的币种）

    为什么要自己写：面板那套 _icon_color 是给「米色卡片上的价格」用的，靠最大饱和色斑；
    研究格子的底色是彩色立绘（野蛮人的黄头发、药水的蓝色），最大色斑常常落在立绘上。
    这里改成在固定窗口里数三类的像素票数（实测：圣水 680±30 票、暗黑 500~2000 票且
    彩色票近 0、金币格子的立绘黄头发会混进 400~500 票，所以金币要多加一条「压过圣水」）。
    """
    return icon_kind_win(im, cx + 40, cx + 125, cy - 22, cy + 22)


def _as_price(b):
    """OCR 块 → 价格候选（只认「价格那一行」上的纯数字）"""
    txt = b["t"].replace(" ", "")
    if _has_cjk(txt) or not DIGITS_RE.match(txt):
        return None
    n = _num(txt)
    if n is None or len(txt.replace(" ", "")) < 4 or n < 200:
        return None
    row = 0 if abs(b["y"] - ROW_Y[0]) <= 34 else (1 if abs(b["y"] - ROW_Y[1]) <= 34 else None)
    if row is None:
        return None
    return dict(cx=b["x"], cy=b["y"], row=row, price=n, digits=txt, conf=b["c"],
                level=None, bbox=b.get("bbox") or (b["x0"], b["x1"], b["y0"], b["y1"]),
                src="white" if b.get("_white") else "color")


def _strip_levels(im, prices, bw=64, bh=52, scale=4, dx=75, dy=78):
    """每个价格格左上方的「等级方块」拼成一条横幅，一次 OCR 全读掉（12 次单图 OCR 太慢）"""
    if not prices:
        return {}
    crops = []
    for p in prices:
        x0, y0 = int(p["cx"] - dx), int(p["cy"] - dy)
        c = im[max(0, y0):max(0, y0) + bh, max(0, x0):max(0, x0) + bw]
        if c.shape[0] < bh or c.shape[1] < bw:
            c = cv2.copyMakeBorder(c, 0, max(0, bh - c.shape[0]), 0, max(0, bw - c.shape[1]),
                                   cv2.BORDER_CONSTANT, value=(0, 0, 0))
        crops.append(cv2.resize(c, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
    strip = np.hstack(crops)
    lv = {}
    for b in ocr_boxes(strip, (0, 0, strip.shape[1], strip.shape[0])):
        idx = int((b["x"] / scale) // bw)
        d = "".join(ch for ch in b["t"] if ch.isdigit())
        if d and len(d) <= 2 and idx not in lv:
            lv[idx] = int(d)
    return lv


def parse_page(im):
    """一屏研究对象 → [{price, level, currency, tap, locked, row, page}]（按行、列排好）

    三条通道各干一件事：
      ① 白字掩码 OCR（主）读价格 —— 彩色底上最稳
      ② 彩色 OCR（补）读价格 + 「需要11级实验室」这类锁定提示 —— 白字掩码会把中文读花
      ③ 等级方块拼条一次 OCR —— 深色方块里的白数字，单独拼成一张图才认得准
    """
    grid = (POP_X0 + 2, GRID_Y0, POP_X1 - 2, GRID_Y1)
    white = _white_boxes(im, grid)
    for b in white:
        b["_white"] = True
    color = ocr_boxes(im, grid)

    prices, locked = [], []
    for b in white + color:
        p = _as_price(b)
        if p:
            prices.append(p)
        elif _has_cjk(b["t"]) and ("实验室" in b["t"] or "验室" in b["t"] or "解锁" in b["t"]):
            locked.append(b)
    # 同一格只留一个价：白字优先，其次置信度高的
    prices.sort(key=lambda t: (t["row"], t["cx"], 0 if t["src"] == "white" else 1, -t["conf"]))
    merged = []
    for p in prices:
        dup = [m for m in merged if m["row"] == p["row"] and abs(m["cx"] - p["cx"]) <= 80]
        if dup:
            continue
        merged.append(p)
    merged.sort(key=lambda t: (t["row"], t["cx"]))

    lv = _strip_levels(im, merged)
    for i, p in enumerate(merged):
        p["level"] = lv.get(i)
        p["currency"] = icon_kind(im, p["cx"], p["cy"])
        p["tap"] = [int(p["cx"]), int(p["cy"] - 55)]
        p["edge"] = bool(p["cx"] < POP_X0 + 70 or p["cx"] > POP_X1 - 70)
        p["locked"] = False
    for b in locked:
        lx, ly = b["x"], b["y"]
        if any(abs(m["cx"] - lx) <= 80 and m["row"] == (0 if abs(ly - ROW_Y[0]) <= 60 else 1)
               for m in merged):
            continue
        merged.append(dict(cx=lx, cy=ly, row=0 if abs(ly - ROW_Y[0]) <= 60 else 1,
                           price=None, digits="", currency=None, level=None, locked=True,
                           tap=None, edge=bool(lx < POP_X0 + 70 or lx > POP_X1 - 70)))
    merged.sort(key=lambda t: (t["row"], t["cx"]))
    return merged


KNOWN_LABELS = ("信息", "升级", "研究", "提速", "指派", "配制", "训练", "更多", "取消",
                "选择整行", "壁垒之戒", "开始", "查看")


def labels_good(labels):
    """底栏标签认出来了没：至少有一个能对上已知词表。

    面板刚弹出时字还没渲染完，这一帧 OCR 出来的是一堆乱码（实测「研究」被读成「咖九」），
    拿这种帧去定「研究」卡的位置会点到隔壁去，所以要判一下再决定要不要重抓。
    """
    return any(any(k in t for k in KNOWN_LABELS) for _x, t in labels)


def find_lab_template(im, min_score=0.70):
    """在画面上用贴图找实验室 → (x, y, scale, score) 或 None

    为什么要这一手：游戏一开始研究/施工会把镜头自动推到那栋建筑上，镜头一挪，
    anchors 那 3 张锚点贴图就可能在画面外，calibrate() 只能退回缓存坐标 → 后面全点偏
    （实测连点三次点到隔壁正在施工的建筑上）。实验室贴图是常驻的，认到它就能反推平移量。
    """
    tmpl = cv2.imread(LAB_TMPL)
    if tmpl is None or im is None:
        return None
    best = None
    for sc in (0.94, 0.97, 1.0, 1.03, 1.06, 1.09):
        t = cv2.resize(tmpl, None, fx=sc, fy=sc)
        if t.shape[0] >= im.shape[0] or t.shape[1] >= im.shape[1]:
            continue
        r = cv2.matchTemplate(im, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, ml = cv2.minMaxLoc(r)
        if best is None or mx > best[3]:
            best = (ml[0] + t.shape[1] // 2, ml[1] + t.shape[0] // 2, sc, float(mx))
    return best if best and best[3] >= min_score else None


def fix_calib_from_template(bld, T=None, im=None, min_score=0.70):
    """用实验室贴图把镜头平移量反解出来并写回标定 → 新 T 或 None

    干什么用：游戏会自己推镜头（开始研究/施工、弹商店、切页面回来都会），而村庄锚点贴图
    一旦不在画面里，`calibrate()` 就只能退回上一次的缓存坐标。缓存一旦过期，采集点、
    升级点击、实验室点击**全部**开始点偏（实测采集点偏了 100px，点到隔壁实验室上）。
    实验室贴图是常驻的，认到它就能算出平移量，顺手把标定修回去。
    """
    im = im if im is not None else mem.grab()
    hit = find_lab_template(im, min_score=min_score)
    if not hit:
        return None
    mx, my, sc, score = hit
    try:
        s_cap = float((jload(LAB_TMPL_META, {}) or {}).get("s_capture") or 1.0)
    except Exception:
        s_cap = 1.0
    s_new = s_cap * sc
    try:
        rows = bld.snapshot()
    except Exception:
        return None
    lab = [r for r in rows if r["name"] == "Laboratory" and r["village"] == "home"]
    if not lab:
        return None
    r = lab[0]
    cx, cy = bld.to_calib(r["x"], r["y"])
    dx, dy = bldlib.tap_off_of("Laboratory", bldlib.load_offsets())
    tx, ty = (mx - s_new * cx) - dx, (my - s_new * cy) - dy
    T = dict(s=round(s_new, 5), tx=round(tx, 2), ty=round(ty, 2),
             matches=1, anchor_score=round(score, 3), via="lab_template",
             at=int(time.time()))
    mem.save_calib(T)
    return T


def is_village(im):
    """画面是不是「干净的村庄」：左上空地是草地（绿），且没有弹窗/建筑面板。

    为什么要这个判据：从「战斗/联机模式」页按 BACK 回到村庄时镜头会平移几十像素
    （实测 tx 从 -31.7 跳到 +54.5），拿旧标定去点实验室会点到隔壁一栋正在施工的建筑上，
    然后一路下去全错。所以每次动手前先确认「我在村庄，且镜头已按新画面重新标定」。
    """
    if im is None:
        return False
    h, w = im.shape[:2]
    q = im[int(h * 0.14):int(h * 0.55), 0:int(w * 0.24)]
    hsv = cv2.cvtColor(q, cv2.COLOR_BGR2HSV)
    green = float(((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85)
                   & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 60)).mean())
    return green > 0.22


def top_title(im):
    """弹窗顶部标题栏的中文标题 → str 或 None

    这是最稳的「有没有弹窗」判据：研究弹窗的标题是「选择要升级的对象」/「正在升级中」，
    确认页是「将<兵种>升至<N>级？」；而它在两种弹窗上的位置完全一样（y 20~110，居中）。
    """
    if im is None:
        return None
    for b in ocr_boxes(im, (560, 15, 1650, 118)):
        if _has_cjk(b["t"]) and len(b["t"]) >= 3:
            return b["t"]
    return None


def popup_x(im):
    """画面右上角那个红色圆角 X（弹窗/确认页的关闭键）→ (x, y) 或 None

    这是「有没有模态弹窗」最干净的判据：实测干净村庄 / 实验室面板 / 战斗页都没有这个红斑，
    而研究弹窗（1755,64）和研究确认页（1847,85）各有一个 60~70px 的红圆角方块。
    比「顶部一大片浅色」靠谱——「正在升级中」那条绿色横幅会把浅色判据带瞎。
    """
    if im is None or im.shape[0] < 300 or im.shape[1] < 1900:
        return None
    # y 放宽到 280：商店/礼包这类推广弹层的 X 比研究弹窗低一截（实测 (1830,180)）
    sub = im[15:280, 1650:1960]
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(int)
    sat = hsv[:, :, 1].astype(int)
    v = hsv[:, :, 2].astype(int)
    m = ((((h <= 8) | (h >= 170)) & (sat > 120) & (v > 120))).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for k in range(1, n):
        x, y, w, hh, area = stats[k]
        if not (area >= 1500 and 40 <= w <= 110 and 40 <= hh <= 110):
            continue
        # 红块中间必须有个白色的「✕」：否则会把底栏红角标、资源条红点这类纯色块也当成关闭键
        box = sub[y:y + hh, x:x + w]
        white = int(((box[:, :, 0] > 195) & (box[:, :, 1] > 195)
                     & (box[:, :, 2] > 195)).sum())
        if white < max(60, area * 0.04):
            continue
        if best is None or area > best[0]:
            best = (int(area), int(x + 1650 + w // 2), int(y + 15 + hh // 2))
    return (best[1], best[2]) if best else None


def is_popup(im):
    """画面上是不是有个模态弹窗 / 确认页（右上角有红 X，或者认出了确认页的绿色按钮）"""
    if im is None:
        return False
    if popup_x(im):
        return True
    return bool(panel.confirm_page(im).get("open"))


def edges(im):
    """粗判画面：研究弹窗 / 建筑面板 / 村庄"""
    if im is None:
        return dict(popup=False, panel=False)
    pan = panel.read(im)
    h, w = im.shape[:2]
    top = im[int(h * 0.10):int(h * 0.30), int(w * 0.25):int(w * 0.75)]
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    light = float(((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 120)).mean())
    return dict(popup=light > 0.25, panel=bool(pan.get("open")),
                n_cards=len(pan.get("cards") or []), title_level=pan.get("title_level"))


def pan_to(s, sx, sy, cx=1109, cy=540):
    """把屏幕上的某点拖进画面中央附近（点住空白拖动 = 平移镜头）"""
    dx = int(max(-420, min(420, sx - cx)))
    dy = int(max(-260, min(260, sy - cy)))
    s.run("input swipe %d %d %d %d 300" % (cx, cy, cx + dx, cy + dy))
    time.sleep(0.9)


# ---------------------------------------------------------------- 主体
class Research:
    def __init__(self, s=None, bld=None, log=None, quiet=False, cfg=None):
        self.s = s or mem.shell()
        self.bld = bld or bldlib.Buildings(s=self.s)
        self.log = log or (lambda *a: None)
        self.quiet = quiet
        self.cfg = cfg or {}
        self.ast = jload(STATE, {}) or {}
        self.catalog = jload(CACHE, {}) or {}
        self.shots = []
        self.page_cur = 0
        self.page_sigs = []
        self.pages = []
        self.cand_all = []

    # ---------------- 参数 ----------------
    def opt(self, k, d=None):
        v = self.cfg.get(k)
        return d if v is None else v

    def caps(self):
        """每个币种的单次研究上限（config 可调）"""
        return {"gold": int(self.opt("max_cost_gold", 3_000_000)),
                "elixir": int(self.opt("max_cost_elixir", 6_000_000)),
                "dark": int(self.opt("max_cost_dark", 100_000))}

    def allowed(self):
        kinds = self.opt("upgrade_kinds", ["elixir", "dark"]) or []
        return [k for k in ("elixir", "dark", "gold") if k in kinds]

    def prio(self):
        """币种优先级；research_alternate 打开时「上一笔用的币种」排到最后。

        为什么轮换：实验室只有 1 个槽位，圣水一满仓就永远排第一，暗黑重油（也有上限）
        就会一直花不出去。轮换一下两种资源都能消化掉，代价只是顺序，不影响上限。
        """
        p = list(self.opt("research_currency_priority") or ["elixir", "dark", "gold"])
        if self.opt("research_alternate", True):
            last = (self.record() or {}).get("currency")
            if last in p and len(p) > 1:
                p = [c for c in p if c != last] + [last]
        return {c: i for i, c in enumerate(p)}

    # ---------------- 截图 ----------------
    def grab(self, tries=3):
        """抓一帧，带 PNG 完整性校验。

        为什么不用 mem.grab()：它优先走 minicap（socket 流），偶尔把两帧拼在一起或者截断，
        cv2 只在 stderr 抱怨一句 libpng error 就返回一张「尺寸对、内容错」的图——
        那种图上认面板/认价格全是错的，而且不会抛异常。这里直接 screencap + 校验 IEND 尾巴。
        """
        p = "/tmp/_res_cur.png"
        for _ in range(max(1, tries)):
            try:
                with open(p, "wb") as f:
                    r = mem._adb(["exec-out", "screencap", "-p"], timeout=30, stdout=f,
                                 capture_output=False)
                if getattr(r, "returncode", 0) == 0:
                    with open(p, "rb") as f:
                        blob = f.read()
                    if len(blob) > 100_000 and b"IEND" in blob[-16:]:
                        im = cv2.imread(p)
                        if im is not None and im.shape[0] >= 900 and im.shape[1] >= 1600:
                            return im
            except Exception:
                pass
            time.sleep(0.4)
        return mem.grab()

    def shot(self, tag, im=None):
        im = self.grab() if im is None else im
        if im is None:
            return None
        os.makedirs(LOGD, exist_ok=True)
        p = os.path.join(LOGD, "res_%s_%s.jpg" % (
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), tag))
        cv2.imwrite(p, im, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
        self.shots.append(p)
        return p

    # ---------------- 关面板 ----------------
    def ensure_village(self, max_try=3):
        """把画面弄回干净村庄：弹窗按 X、建筑面板取消选中、别的整屏页面按 BACK。

        「战斗/联机模式」这类整屏 UI 只有 BACK 能退，而 BACK 在村庄会弹「确定退出游戏」，
        所以顺序必须是「先认画面类型，再决定按什么」，不能无脑 BACK。
        """
        for _ in range(max_try):
            # 先保证「屏幕亮 + 游戏在前台」：手机 10 分钟没操作会自动熄屏（实测这一条
            # 让整轮研究全废：黑屏上认不出村庄，按 BACK 也不顶用，还会把后面全带偏）
            if not mem.awake():
                self.log("  屏幕熄了，先唤醒")
                self.s.run("input keyevent KEYCODE_WAKEUP")
                time.sleep(1.2)
                self.s.run("wm dismiss-keyguard")      # 有锁屏就划掉（不涉及任何密码）
                time.sleep(0.6)
            if not mem.awake():
                self.log("  唤醒失败，放弃本轮研究")
                return False
            try:
                if mem.PKG not in mem.focus():
                    self.log("  游戏不在前台，拉回前台")
                    self.s.run(f"am start -n {mem.PKG}/{mem.ACT}")
                    time.sleep(4.0)
            except Exception:
                pass
            im = self.grab(tries=2)
            if im is None:
                return False
            if is_popup(im):
                self.close_popup(im)
                continue
            pan = panel.read(im)
            if pan.get("open") and pan.get("cards"):
                # 关建筑面板只用「再点一次同一栋」这个 toggle：
                # coc_patrol2.clear_panels 兜底会按 BACK，而在村庄按 BACK 会弹「确定退出游戏」，
                # 它再去 OCR 找「取消」时点到过别的地方（实测点开了「战斗」→ 联机模式页）。
                pos = getattr(self, "last_building_tap", None) or (self._lab_tap() or (None,))
                if pos[0] is not None:
                    self.s.tap(pos[0], pos[1], wait=0.9)
                continue
            if not is_village(im):
                self.log("  画面不是村庄（整屏 UI/黑屏），按 BACK 退出")
                if os.environ.get("COC_DEBUG"):
                    self.log("   debug: awake=%s focus=%s" % (mem.awake(), mem.focus()[:80]))
                self.s.run("input keyevent 4")
                time.sleep(1.1)
                continue
            return True
        return False

    def close_all(self):
        """收尾：把画面弄回干净村庄（弹窗按 X / 面板取消选中 / 整屏 UI 按 BACK）。"""
        self.ensure_village()

    def close_popup(self, im=None):
        """关掉当前弹窗：直接点画面上认出来的那个红 X → bool"""
        im = im if im is not None else self.grab(tries=1)
        if im is None:
            return False
        pos = popup_x(im)
        if pos:
            self.s.tap(*pos, wait=0.9)
            return True
        if panel.confirm_page(im).get("open"):
            self.s.tap(*X_CLOSE_CONFIRM, wait=0.9)
            return True
        return False

    def close_confirm(self):
        """关掉当前确认页 / 弹窗。X 的位置分两种，认错会点到村庄里的金币栏（会开商城）。"""
        return self.close_popup()

    # ---------------- 开面板 ----------------
    def _lab_tap(self, force=False):
        """实验室的点击点 → (x, y, src)

        两条路：
          ① 镜头标定可信（anchors/bubbles 命中 ≥5）→ 直接用世界坐标投影，最准；
          ② 不可信（缓存/identity，或者锚点根本没在画面里）→ 用实验室贴图找它在画面哪儿，
             再用「已知缩放 + 实验室的世界坐标」反解平移量，顺手把标定修正掉。
        绝不「平移镜头」去找实验室：那次平移把镜头拖到村庄另一头，后面每一步都点偏。
        """
        try:
            T = mem.calibrate(self.grab())
            rows = self.bld.snapshot()
        except Exception as e:
            self.log(f"  [research] 读建筑池失败: {e}")
            return None
        lab = [r for r in rows if r["name"] == "Laboratory" and r["village"] == "home"]
        if not lab:
            return None
        r = lab[0]
        self.lab_level = r["level"]
        dx, dy = bldlib.tap_off_of("Laboratory", bldlib.load_offsets())
        trust = ((T or {}).get("via") in ("anchors", "anchors+bubbles", "bubbles")
                 and (T or {}).get("matches", 0) >= 5 and not force)
        if trust:
            sx, sy = self.bld.screen(r["x"], r["y"], T)
            if 30 <= sx <= 2190 and 150 <= sy <= 950:
                return (sx + dx, sy + dy, "calib")
        # ---- 兜底：贴图定位 ----
        hit = find_lab_template(self.grab())
        if not hit:
            self.log("  [research] 镜头标定不可信，贴图也没找到实验室")
            return None
        mx, my, sc, score = hit
        fixed = fix_calib_from_template(self.bld, T, im=self.grab())
        self.tmpl_fix = fixed
        if fixed:
            self.log("  [research] 贴图定位实验室 @(%d,%d) 分数 %.2f → 修正标定 "
                     "s=%.4f tx=%.1f ty=%.1f" % (mx, my, score, fixed["s"], fixed["tx"],
                                                 fixed["ty"]))
        return (mx, my, "template")

    def open_lab_panel(self):
        """点实验室 → {open, labels, research_card, busy, title_ok, jpg}

        打开后要用「标题等级 == 内存里实验室的等级」验一下：镜头一偏就会点到旁边的建筑，
        那种面板也有价格、也有卡片，不验等级会一路错下去（实测点到过正在施工的法师塔）。
        """
        for attempt in range(2):
            if attempt:
                self.log("  [research] 面板标题对不上实验室，重新标定镜头后再点一次")
                time.sleep(0.6)
            self.ensure_village()
            hit = self._lab_tap(force=bool(attempt))
            if not hit:
                return dict(open=False, why="no_lab_building", labels=[], research_card=None)
            pos, src = (hit[0], hit[1]), hit[2]
            self.last_building_tap = pos
            pan, im, labels = {"open": False}, None, []
            self.last_building_tap = pos
            for i in range(2):
                self.s.tap(*pos, wait=0.8)
                # 点完先等它画出来（面板有弹出动画），别急着判「没开」——
                # 判没开就会再点一次，那一下正好是 toggle，把刚开的面板关掉
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    im = self.grab(tries=1)
                    if im is not None:
                        pan = panel.read(im)
                        labels = self._labels(im)
                        if pan.get("open"):
                            break
                    time.sleep(0.5)
                if pan.get("open"):
                    break
            if im is None:
                return dict(open=False, why="no_frame", labels=[], research_card=None)
            lvl = pan.get("title_level")
            if not pan.get("open") or lvl is None or lvl == getattr(self, "lab_level", None):
                break
            self.s.tap(*pos, wait=0.8)          # 点到别的建筑了：再点一下取消选中
        card, by = None, None
        for x0, txt in labels:
            if "研究" in txt:
                card, by = x0, "ocr"
                break
        if card is None and (pan.get("cards") or []):
            # OCR 没认出来（小字+描边偶尔糊）：退成最右一张卡。
            # 不能就这么判成「在研究中」——那会让实验室永远不动。
            card, by = pan["cards"][-1]["cx"], "rightmost"
        out = dict(open=bool(pan.get("open")),
                   labels=[t for _, t in labels],
                   title_level=pan.get("title_level"),
                   title_ok=(pan.get("title_level") == getattr(self, "lab_level", None)),
                   n_cards=len(pan.get("cards") or []), src=src,
                   research_card=card, card_by=by, tap=list(pos),
                   jpg=self.shot("lappanel", im))
        # 贴图定位出来的点在标题对上之后才敢用来修正标定：认错建筑的话这一步会把标定带偏
        if out["open"] and out["title_ok"] and src == "template" and getattr(self, "tmpl_fix", None):
            mem.save_calib(dict(self.tmpl_fix, via="lab_template", matches=1))
            out["calib_fixed"] = self.tmpl_fix
            self.log("  [research] 已用实验室贴图修正镜头标定：%s" % (self.tmpl_fix,))
        out["busy"] = bool(card is None and out["open"])
        if out["open"] and not out["title_ok"]:
            out["why"] = "wrong_building(title=%s want=%s)" % (
                pan.get("title_level"), getattr(self, "lab_level", None))
        return out

    def _labels(self, im):
        """底栏卡片标签 → [(x, text)]（按 x 排好）

        实测同一帧换条带差别巨大：裁 895~980 时「研究」被读成「咻九」，裁 885~992 就 0.98 认对。
        所以两条带都读一遍，取「命中已知词表最多」的那条。
        """
        best, best_hit = [], -1
        for y0, y1 in (LABEL_BAND, LABEL_BAND_ALT):
            band = im[y0:y1, 420:1820]
            got = sorted((int(b["x"]) + 420, b["t"])
                         for b in ocr_boxes(band, (0, 0, band.shape[1], band.shape[0])))
            hit = sum(1 for _x, t in got if any(k in t for k in KNOWN_LABELS))
            if hit > best_hit:
                best, best_hit = got, hit
        return best

    def open_research_ui(self, pan=None):
        """点面板上的「研究」卡 → 研究弹窗"""
        pan = pan or self.open_lab_panel()
        if not pan.get("open") or pan.get("research_card") is None:
            return False
        self.s.tap(pan["research_card"], LABEL_Y, wait=2.0)
        im = self.grab()
        self.ui_title = top_title(im) if im is not None else None
        if im is None or not is_popup(im):
            self.log("  [research] 没打开研究弹窗（标题认不到）")
            return False
        if self.ui_title and ("升级中" in self.ui_title or "正在" in self.ui_title):
            # 标题变成「正在升级中」= 实验室那 1 个槽位已经被占着
            self.log("  [research] 研究界面标题是「%s」→ 实验室在研究中" % self.ui_title)
            self.busy_from_ui = True
            self.busy_info = self.read_busy_banner(im)
            self.close_popup(im)
            return False
        self.busy_from_ui = False
        self.page_cur, self.page_sigs = 0, [_sig(im)]
        self.pages = [dict(page=0, n=0, sig=self.page_sigs[0])]
        self.popup_jpg = self.shot("popup", im)
        return True

    def read_busy_banner(self, im):
        """「正在升级中」那条绿横幅 → {name, level, hours, text}（只用于日志和状态记录）"""
        out = dict(name=None, level=None, hours=0, text=None)
        for b in ocr_boxes(im, (760, 120, 1780, 380)):
            t = b["t"]
            m = TITLE_RE.search(t)
            if m:
                out["name"], out["level"] = m.group(1), int(m.group(2))
            elif re.search(r"[\u4e00-\u9fff]+[（(]\d+级[)）]", t):
                mm = re.search(r"([\u4e00-\u9fff]+)[（(](\d+)级[)）]", t)
                if mm:
                    out["name"], out["level"] = mm.group(1), int(mm.group(2))
            h = TIME_RE.search(t)
            if h and (h.group(1) or h.group(2)) and not out["hours"]:
                out["hours"] = int(h.group(1) or 0) * 24 + int(h.group(2) or 0)
            if out["text"] is None and _has_cjk(t) and len(t) >= 4:
                out["text"] = t
        return out

    # ---------------- 翻页 ----------------
    def swipe(self, right=False):
        self.s.run("input swipe %d %d %d %d %d" % (SW_R if right else SW_L))
        time.sleep(0.9)
        return self.grab()

    def reset_left(self, max_n=None):
        """一路向右扫回最左边（转盘在左端会夹住，位置是确定的）。

        为什么要「先回到底再数页」：这个弹窗是像素级滚动、不吸附页边界，往回扫和往前扫
        落点不一定一样；从夹住的左端开始数 n 次，落点才和扫描时一致（扫描也是从左端开始）。
        """
        max_n = max_n or int(self.opt("research_max_pages", MAX_PAGES)) + 1
        prev = None
        for _ in range(max_n):
            im = self.swipe(right=True)
            if im is None:
                return None
            sig = _sig(im)
            if prev is not None and sig == prev:
                break                       # 已经夹在左端动不了了
            prev = sig
        self.page_cur = 0
        return mem.grab()

    def locate(self, price, cx, max_pages=None):
        """从最左边开始一路往右翻，找到「价格 == price 且位置接近 cx」的那一格 → (im, tile)

        为什么不用「第几页」定位：这个弹窗是像素级滚动，`input swipe` 每次落点都不一样
        （同一页扫两遍，格子数会从 7 变 6），按页号回翻必然对不上。按内容找才是稳的。
        """
        max_pages = max_pages or int(self.opt("research_max_pages", MAX_PAGES)) + 2
        im = self.reset_left()
        if im is None:
            return None, None
        prev = None
        for _ in range(max_pages):
            page = parse_page(im)
            hit = [t for t in page if t.get("price") == price
                   and abs(t["cx"] - cx) <= 160 and not t.get("locked")]
            if hit:
                return im, min(hit, key=lambda t: abs(t["cx"] - cx))
            sig = _sig(im)
            if prev is not None and sig == prev:
                break
            prev = sig
            im = self.swipe()
            if im is None:
                break
        return None, None

    def scan(self, im0=None, max_pages=None):
        """一页页读完所有研究对象（从左端开始，翻到画面不再变化为止）→ (tiles, pages)"""
        max_pages = max_pages or int(self.opt("research_max_pages", MAX_PAGES))
        tiles, prev, im, empty = [], None, im0, 0
        self.pages = []
        for i in range(max_pages):
            if im is None:
                im = self.grab()
            if im is None:
                break
            sig = _sig(im)
            if prev is not None and sig == prev:
                break
            prev = sig
            page = parse_page(im)
            for t in page:
                t["page"] = i
            self.page_sigs.append(sig)
            self.pages.append(dict(page=i, n=len(page), sig=sig))
            tiles.extend(page)
            self.page_cur = i
            empty = 0 if page else empty + 1
            if i + 1 >= max_pages or empty >= 2:
                break
            im = self.swipe()
        return tiles, self.pages

    def enrich(self, tiles):
        """用缓存补名字（格位指纹 = 价格|等级；名字只是日志友好，不参与决策）"""
        for t in tiles:
            c = self.catalog.get(self.key_of(t))
            if c:
                t["name"] = c.get("name")
                if t.get("level") is None:
                    t["level"] = c.get("level")
                if t.get("currency") is None:
                    t["currency"] = c.get("currency")
        return tiles

    @staticmethod
    def key_of(t):
        return "%s|%s" % (t.get("price"), t.get("level"))

    # ---------------- 挑目标 ----------------
    def pick(self, tiles, res):
        """可研究 = 未锁定 + 价格读得出 + 币种允许 + 不超该币种上限 + 库存够。

        排序：① 币种已知的排在币种未知的前面（未知的只能靠确认页兜底）
              ② 币种优先级（圣水满仓 → 圣水第一；暗黑量少 → 第二，但仍受便宜档上限约束）
              ③ 可研究的等级高的优先（用户口径：优先做「可研究的最高级、价格付得起」）
              ④ 价格低的优先
        """
        caps, prio, ok = self.caps(), self.prio(), self.allowed()
        cand = []
        for t in tiles:
            t.pop("skip", None)
            if t.get("locked") or not t.get("price"):
                t["skip"] = "locked_or_no_price"
                continue
            cur = t.get("currency")
            if cur is None:
                if t["price"] <= max(caps.get(c, 0) for c in ok):
                    cand.append(t)          # 币种未知：留作后备，确认页再判
                else:
                    t["skip"] = "over_all_caps"
                continue
            if cur not in ok:
                t["skip"] = "currency_%s" % cur
            elif t["price"] > caps.get(cur, 0):
                t["skip"] = "over_cap(%d>%d)" % (t["price"], caps.get(cur, 0))
            elif t["price"] > (res.get(cur) or 0):
                t["skip"] = "insufficient(%d>%d)" % (t["price"], res.get(cur) or 0)
            else:
                cand.append(t)
        lv = lambda t: (t.get("level") if t.get("level") is not None else -1)
        cand.sort(key=lambda t: (t.get("edge", False), t.get("currency") is None,
                                 prio.get(t.get("currency"), 9), -lv(t), t["price"]))
        self.cand_all = cand
        return cand[0] if cand else None

    # ---------------- 执行 ----------------
    def _read_btn(self, im, cf):
        """绿色「确认」按钮上的价格 + 币种图标

        价格必须读全：按钮右下角是币种图标，早先按 x1-70 裁会把最后两位数字切掉
        （2,500,000 读成 25,000，上限校验就形同虚设）。现在裁整个按钮的下半部分，
        只排除图标那一列，并且要求「整串数字」拼出来不小于 4 位。
        """
        x0, x1, y0, y1 = cf["btn_box"]
        cy = (y0 + y1) // 2
        # 两遍读价（两个纵向条带）：OCR 偶尔会多吐一位（实测 2,500,000 被读成 22,500,000），
        # 两遍不一致就取小的那一个——上限校验宁可保守，真扣多少钱随后由内存差值兜底。
        got = []
        for top, bot in ((cy - 8, y1 - 6), (cy - 2, cy + 44)):
            sub = im[max(0, top):max(0, bot), x0 + 8:x1 - 8]
            if not sub.size:
                continue
            big = cv2.resize(sub, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
            limit = (sub.shape[1] - 75) * 3.0          # 图标那一列不参与拼数字
            toks = []
            for b in ocr_boxes(big, (0, 0, big.shape[1], big.shape[0])):
                d = "".join(ch for ch in b["t"] if ch.isdigit())
                if d and b["x"] < limit:
                    toks.append((b["x"], d))
            toks.sort()
            t = "".join(d for _, d in toks)
            if t.isdigit() and 4 <= len(t) <= 9 and not t.startswith("0"):
                got.append(int(t))
        price = None
        if len(got) == 2 and got[0] == got[1]:
            price = got[0]                      # 两遍一致 → 就用它
        elif got:
            g = t.get("price") if isinstance(t, dict) else None
            price = min(got, key=lambda p: abs(p - g)) if g else max(got)
        cur = icon_kind_win(im, x1 - 95, x1 - 5, cy - 25, cy + 25)
        if cur is None:
            cur = panel._icon_color(im, x1 - 85, x1 - 5, cy - 25, cy + 25)
        return price, cur

    def start(self, t, res, dry=False):
        """点格子 → 确认页核对 → 点「确认」→ 内存验扣费"""
        rec = dict(name=t.get("name"), level=t.get("level"), price=t.get("price"),
                   currency=t.get("currency"), page=t.get("page"), tap=t["tap"],
                   at=iso_now())
        if dry:
            rec.update(ok=False, why="dry")
            return rec, res
        tx = int(max(POP_X0 + 70, min(POP_X1 - 70, t["tap"][0])))
        self.s.tap(tx, int(t["tap"][1]), wait=1.5)
        im = self.grab()
        if im is None:
            rec["why"] = "no_frame"
            return rec, res
        rec["confirm_jpg"] = self.shot("confirm_%s" % (t.get("level") or "x"), im)
        cf = panel.confirm_page(im)
        titles = ocr_boxes(im, (500, 20, 1900, 110))
        tname, targ, ttxt = None, None, None
        for b in titles:
            m = TITLE_RE.search(b["t"])
            if m:
                tname, targ, ttxt = m.group(1), int(m.group(2)), b["t"]
                break
        rec["title"], rec["name"], rec["target_level"] = ttxt or (
            titles[0]["t"] if titles else None), tname, targ
        if not cf.get("open") or not cf.get("btn"):
            self.close_confirm()
            rec["why"] = "no_confirm_page"
            return rec, res
        if tname is None:
            self.close_confirm()
            rec["why"] = "no_title"
            return rec, res
        # 顺带记下这次研究的时长（「升级时间 1天」），用来估算实验室什么时候空出来
        for b in ocr_boxes(im, (1500, 900, 1900, 980)):
            m = TIME_RE.search(b["t"])
            if m and (m.group(1) or m.group(2)):
                rec["time_text"] = b["t"]
                rec["hours"] = int(m.group(1) or 0) * 24 + int(m.group(2) or 0)
                break
        price, cur = self._read_btn(im, cf)
        rec["btn_price"], rec["panel_currency"] = price, cur
        grid_price = t.get("price")
        if grid_price and price and abs(price - grid_price) > max(50, grid_price * 0.05):
            # 两处读数不一致（实测 OCR 会把 2,500,000 读成 22,500,000、把 17,000 读成 1,700）：
            # 取小的那个算上限——上限是红线，宁可把这一笔判成「超上限」跳过，也不能放大的过去。
            rec["grid_mismatch"] = "grid=%s btn=%s" % (grid_price, price)
            price = min(grid_price, price)
        elif grid_price and not price:
            price = grid_price
        rec["panel_price"] = price
        caps, ok = self.caps(), self.allowed()
        if price is None:
            why = "no_price"
        elif cur not in ok:
            why = "currency_%s" % cur
        elif price > caps.get(cur, 0):
            why = "over_cap(%d>%d)" % (price, caps.get(cur, 0))
        elif price > (res.get(cur) or 0):
            why = "insufficient(%d>%d)" % (price, res.get(cur) or 0)
        else:
            why = None
        if why:
            self.close_confirm()
            rec["why"] = why
            return rec, res
        # 认下来了 → 写缓存（下次不点进来也知道这格是什么）
        self.catalog[self.key_of(t)] = dict(name=tname, currency=cur, level=targ - 1,
                                            price=price, at=iso_now())
        jsave(CACHE, self.catalog)
        before = mem.read_all(retries=1) or res
        self.s.tap(*cf["btn"], wait=2.2)          # 绿「确认」= 真正扣费
        after = mem.read_all(retries=2) or before
        spent = {c: before[c] - after[c] for c in ("gold", "elixir", "dark")
                 if before.get(c) is not None and after.get(c) is not None}
        paid = {c: v for c, v in spent.items() if v > 0}
        rec.update(ok=bool(paid), paid=paid, before=before, after=after, btn=cf["btn"],
                   after_jpg=self.shot("after", self.grab()))
        rec["match"] = bool(paid.get(cur) and abs(paid[cur] - price) <= 25)
        if not paid:
            rec["why"] = "no_charge"
        elif set(paid) != {cur}:
            rec["alert"] = "wrong_currency_paid: %s" % paid
        return rec, after

    # ---------------- 状态 ----------------
    def save_state(self):
        if self.ast is not None:
            jsave(STATE, self.ast)

    def record(self):
        return (self.ast or {}).get("mem_research") or {}

    def busy_reason(self):
        """实验室是不是已经被占着（1 个槽位）：按自己的记录 + 时长估算"""
        r = self.record()
        if not r:
            return None
        try:
            ts, hours = float(r.get("ts") or 0), float(r.get("hours") or 0)
        except Exception:
            ts, hours = 0, 0
        age_h = (time.time() - ts) / 3600.0
        max_h = float(self.opt("research_max_h", 240) or 240)
        if hours and age_h < hours:
            return "%s（已过 %.1f/%.1f 小时）" % (r.get("name"), age_h, hours)
        if not hours and age_h < max_h:
            return "%s（已过 %.1f 小时，时长未知）" % (r.get("name"), age_h)
        return None


# ---------------------------------------------------------------- 一轮（给 coc_mem_auto 用）
def run(auto, res, dry=False):
    cfg = auto.cfg
    if not cfg.get("research", True):
        return dict(skipped="research_disabled")
    r = Research(s=auto.s, bld=auto.bld, log=auto.log, quiet=auto.quiet, cfg=cfg)
    r.ast = auto.ast
    out = dict(busy=r.busy_reason(), tiles=[], pages=[], picked=None, result=None,
               alerts=[], caps=r.caps(), allowed=r.allowed())
    if out["busy"] and not dry:
        auto.log(f"  [research] 实验室在忙：{out['busy']}，跳过")
        return out
    t0 = time.time()
    try:
        pan = r.open_lab_panel()
        out["panel"] = dict(labels=pan.get("labels"), open=pan.get("open"),
                            busy=pan.get("busy"), tap=pan.get("tap"), jpg=pan.get("jpg"))
        if not pan.get("open"):
            out["alerts"].append("lab_panel_not_open")
            return out
        if pan.get("busy"):
            # 面板上没有「研究」卡 = 实验室已经在研究（或这不是实验室）
            auto.log(f"  [research] 面板没有「研究」卡（{pan.get('labels')}）→ 当作研究中，本轮不动")
            out["busy"] = out["busy"] or "panel_no_research_card"
            if not r.record():
                r.ast["mem_research"] = dict(name="unknown(manual)", ts=int(time.time()),
                                             hours=0, at=iso_now())
                r.save_state()
            return out
        if not r.open_research_ui(pan):
            if getattr(r, "busy_from_ui", False):
                info = getattr(r, "busy_info", {}) or {}
                out["busy"] = "正在升级中：%s（剩下约 %s 小时）" % (
                    info.get("name") or "?", info.get("hours") or "?")
                out["busy_banner"] = info
                auto.log("  [research] 实验室在研究中：%s，本轮不动" % out["busy"])
                if not r.record():
                    r.ast["mem_research"] = dict(
                        name=info.get("name") or "unknown(manual)",
                        to_level=info.get("level"), ts=int(time.time()),
                        hours=info.get("hours") or 0, currency=None,
                        at=iso_now(), note="从「正在升级中」横幅认出来的")
                    r.save_state()
                return out
            out["alerts"].append("research_ui_not_open")
            return out
        tiles, pages = r.scan(im0=r.grab())
        r.enrich(tiles)
        out.update(tiles=tiles, pages=pages)
        picked = r.pick(tiles, res)
        out["picked"] = picked
        if picked is None:
            auto.log("  [research] 没有可研究的（锁着 / 付不起 / 超上限）")
            return out
        auto.log("  [research] 选中 %s L%s→%s %s %s @%s（第%s页，共%s个对象）" % (
            picked.get("name") or "?", picked.get("level"), (picked.get("level") or 0) + 1,
            picked.get("price"), picked.get("currency"), picked["tap"],
            picked.get("page"), len(tiles)))
        if dry:
            rec, _ = r.start(picked, res, dry=True)
            out["result"] = rec
            return out
        im, here = r.locate(picked["price"], picked["cx"])
        if not here:
            out["alerts"].append("tile_not_found")
            auto.log("  [research] 翻遍各页都找不到这个格子（价格 %s），放弃本轮（不点任何格子）"
                     % picked.get("price"))
            return out
        picked = dict(picked, tap=here["tap"],
                      currency=here.get("currency") or picked.get("currency"))
        out["picked"] = picked
        rec, res = r.start(picked, res)
        out["result"] = rec
        if rec.get("alert"):
            out["alerts"].append(rec["alert"])
        if rec.get("ok"):
            r.ast["mem_research"] = dict(
                name=rec.get("name"), from_level=rec.get("level"),
                to_level=rec.get("target_level"), price=rec.get("panel_price"),
                currency=rec.get("panel_currency"), ts=int(time.time()),
                hours=rec.get("hours") or 0, at=iso_now(), paid=rec.get("paid"))
            r.save_state()
            auto.log("  [research] ✓ %s 升到 %s 级，扣 %s（面板 %s %s，匹配 %s）" % (
                rec.get("name"), rec.get("target_level"), rec.get("paid"),
                rec.get("panel_price"), rec.get("panel_currency"), rec.get("match")))
        else:
            auto.log("  [research] 没成功：%s" % rec.get("why"))
        if rec.get("ok") and r.opt("research_learn_busy", True):
            ap = r.open_lab_panel()
            out["after_panel"] = dict(labels=ap.get("labels"), busy=ap.get("busy"),
                                      card_by=ap.get("card_by"), jpg=ap.get("jpg"))
            auto.log("  [research] 研究开始后实验室面板：%s（busy=%s）" % (
                ap.get("labels"), ap.get("busy")))
        out["sec"] = round(time.time() - t0, 1)
        return out
    except Exception as e:
        out["alerts"].append("research_error:%s" % type(e).__name__)
        auto.log(f"  [research] 异常 {type(e).__name__}: {e}")
        return out
    finally:
        try:
            r.close_all()
        except Exception:
            pass
        r.save_state()


def _hours(txt):
    if not txt:
        return 0
    m = TIME_RE.search(txt)
    return (int(m.group(1) or 0) * 24 + int(m.group(2) or 0)) if m else 0


# ---------------------------------------------------------------- CLI
def build_catalog(cfg=None, max_tiles=None):
    """把所有研究对象格子逐个点开，只读「名字 + 币种」写进 research_catalog.json。

    点格子只会打开确认页；不点绿色「确认」一分钱都不花，每个格子读完立刻按 X 退出。
    一次性工作：之后日志和 --dry 里就能直接显示兵种/法术的名字，不用再靠价格猜。
    """
    import coc_config
    cfg = dict(coc_config.load(), **(cfg or {}))
    r = Research(log=print, cfg=cfg)
    n = 0
    try:
        pan = r.open_lab_panel()
        print("panel:", pan.get("labels"), pan.get("open"), pan.get("busy"))
        if not pan.get("open") or pan.get("busy"):
            return 0
        if not r.open_research_ui(pan):
            return 0
        tiles, pages = r.scan(im0=r.grab())
        print("pages:", [(p["page"], p["n"]) for p in pages], "tiles:", len(tiles))
        budget = max_tiles or 999
        done = set()
        im = r.reset_left()
        prev = None
        while im is not None and n < budget:
            page = parse_page(im)
            restart = False
            for t in [x for x in page if not x.get("locked") and x.get("price")]:
                if n >= budget or restart:
                    break
                k = Research.key_of(t)
                if k in r.catalog or (k, round(t["cx"] / 60)) in done:
                    continue
                r.s.tap(int(max(POP_X0 + 70, min(POP_X1 - 70, t["tap"][0]))), t["tap"][1],
                        wait=1.4)
                im2 = r.grab()
                cf = panel.confirm_page(im2) if im2 is not None else {"open": False}
                name, targ = None, None
                for b in (ocr_boxes(im2, (500, 20, 1900, 110)) if im2 is not None else []):
                    m = TITLE_RE.search(b["t"])
                    if m:
                        name, targ = m.group(1), int(m.group(2))
                        break
                price, cur = (r._read_btn(im2, cf) if cf.get("open") else (None, None))
                done.add((k, round(t["cx"] / 60)))
                if name:
                    r.catalog[k] = dict(name=name, currency=cur or t.get("currency"),
                                        level=(targ - 1) if targ else t.get("level"),
                                        grid_price=t["price"], price=price, at=iso_now())
                    jsave(CACHE, r.catalog)
                    n += 1
                    print("  %-12s L%-3s 网格 %-9s 确认页 %-9s %s" % (
                        name, (targ - 1) if targ else "?", t["price"], price,
                        cur or t.get("currency")))
                else:
                    print("  没读到名字", t["price"], t.get("level"))
                if not r.close_confirm():
                    # 认不出来到底开的是什么：先无条件按一次右上角 X（刚点过格子，多半是确认页），
                    # 再确认研究弹窗还在不在，不在就重开并从最左边重扫
                    r.close_popup()
                    im3 = r.grab()
                    if im3 is None or not is_popup(im3):
                        print("  ⚠ 弹窗丢了，重开研究界面并从最左边重扫")
                        if not r.open_research_ui():
                            return n
                        restart = True
                        break
                im = r.grab()
            if restart:
                im = r.reset_left()
                prev = None
                continue
            sig = _sig(im)
            if prev is not None and sig == prev:
                break
            prev = sig
            im = r.swipe()
        print("新记下 %d 个对象，缓存 %d 条" % (n, len(r.catalog)))
        return n
    finally:
        r.close_all()
        mem.close()


def dump_plan(cfg=None):
    """--dry：把研究对象一屏屏打出来，不点任何格子"""
    import coc_config
    cfg = dict(coc_config.load(), **(cfg or {}))
    r = Research(log=print, cfg=cfg)
    try:
        res = mem.read_all(retries=2) or {}
        busy = r.busy_reason()
        print(json.dumps(dict(res=res, busy=busy), ensure_ascii=False))
        pan = r.open_lab_panel()
        print(json.dumps(dict(panel=dict(labels=pan.get("labels"), open=pan.get("open"),
                                         busy=pan.get("busy"), tap=pan.get("tap"))),
                         ensure_ascii=False))
        if pan.get("busy") or not pan.get("open"):
            return
        if not r.open_research_ui(pan):
            return
        tiles, pages = r.scan(im0=r.grab())
        r.enrich(tiles)
        picked = r.pick(tiles, res)
        rows = [dict(page=t["page"], lock=True, cx=t.get("cx")) if t.get("locked") else
                dict(page=t["page"], name=t.get("name"), level=t.get("level"),
                     price=t.get("price"), currency=t.get("currency"),
                     skip=t.get("skip"), tap=t["tap"]) for t in tiles]
        plan = dict(res=res, n=len(tiles), pages=pages, tiles=rows,
                    picked=picked, caps=r.caps(), allowed=r.allowed())
        os.makedirs(LOGD, exist_ok=True)
        out = os.path.join(LOGD, "research_plan.json")
        jsave(out, plan)
        print(json.dumps(plan, ensure_ascii=False, indent=1, default=float))
        print("计划已存", out)
    finally:
        r.close_all()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只读不点：打印研究对象清单")
    ap.add_argument("--catalog", action="store_true",
                    help="把研究对象格子逐个点开只读名字（不点确认，不花钱）")
    ap.add_argument("--max-tiles", type=int, default=None, help="--catalog 最多点几个")
    a = ap.parse_args()
    import coc_config
    cfg = coc_config.load()
    if a.catalog:
        build_catalog(cfg, max_tiles=a.max_tiles)
        return 0
    if a.dry:
        dump_plan(cfg)
        mem.close()
        return 0
    # 真跑：和 coc_mem_auto 里走同一条路径（run()），只是不进整轮巡检
    class _Shim:
        pass
    sh = _Shim()
    sh.cfg, sh.log, sh.quiet = cfg, print, False
    sh.s, sh.bld = mem.shell(), None
    sh.ast = jload(STATE, {}) or {}
    try:
        out = run(sh, mem.read_all(retries=2) or {}, dry=False)
        print(json.dumps(out, ensure_ascii=False, default=float, indent=1))
        jsave(STATE, sh.ast)
        return 0 if (out.get("result") or {}).get("ok") else 1
    finally:
        mem.close()


if __name__ == "__main__":
    sys.exit(main())
