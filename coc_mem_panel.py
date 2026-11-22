#!/usr/bin/env python3
"""建筑面板识别：判断面板有没有开、找到「升级」按钮、读面板上的价格和币种、认确认页。

为什么要有这一层
  升级链是「点建筑 → 面板 → 点升级 → 确认页 → 点确认」。之前「升级」按钮用的是固定坐标，
  面板两按钮（信息/升级）和三按钮（信息/升级/训练）时按钮位置不同，固定坐标会落到
  别的按钮上（实测会点到「编辑军队」）。这里改成从画面几何算出来：
  面板卡片总是在屏幕底部一排、宽度 178、间距 206、整体水平居中于 x=1109；
  「升级」是第 2 张卡（x 升序）。价格显示在那张卡正上方，所以「价格数字的中心 x」
  反过来又能确认哪张卡是升级卡 —— 两边互相验证。

坐标依据（19.100.1，2220x1080）
  panel3.png（训练营，3 按钮）卡片 813-989 / 1022-1197 / 1230-1406，底边 929
  k_panel.png（隐形炸弹，2 按钮）卡片 917-1094 / 1125-1300，底边 929
  价格数字 底座 752-790（= 底边-177），高度 15-16，宽 12-17

这个模块是「只认字、不做决定」的一层：唯一决定就是 read() 返回值里的 up_btn / price /
currency / title_level，三者必须互相印证才能点（校验在 coc_mem_auto 里）。

用法：
  python3 coc_mem_panel.py 截图.png         # 打印识别结果
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

HAMMER = os.path.join(HERE, "coc_templates", "panel_hammer.png")   # 「升级」按钮上的锤子
DIGITS = os.path.join(HERE, "coc_templates", "digits16x22.npz")     # 16x22 归一化数字模板
GW, GH = 16, 22
_HT = {}
_DT = {}


# ---------------------------------------------------------------- 数字模板
def norm(g):
    """字形 → 单位长度向量（比较用余弦相似度，对亮度不敏感）"""
    v = np.asarray(g, dtype=np.float32).reshape(-1)
    s = float(np.linalg.norm(v))
    return v / s if s else v


def load_digits():
    """{数字: [模板向量…]}；每个数字可能有多份样本（不同底色/抗锯齿）"""
    if not _DT:
        z = np.load(DIGITS)
        _DT.update({k: z[k] for k in z.files})
    return _DT


def find_hammer(im):
    """在底部面板带里找「升级」按钮上的那把锤子 → (x, y, score) 或 None

    为什么用锤子图标定按钮：面板有 2/3/5 张卡，卡序不固定（城墙那张是
    信息/选择整行/升级更多/升级/壁垒之戒），而且「改装」卡上也有价格牌，
    光靠「价格牌对齐某张卡」会认错卡（实测把箭塔的 2,000,000 读成了改装价的 3,000,000）。
    锤子是固定贴图，全型号面板都能 1.000 命中，直接用它定「升级」按钮。
    """
    if "t" not in _HT:
        t = cv2.imread(HAMMER)
        _HT["t"] = None if t is None else cv2.cvtColor(t, cv2.COLOR_BGR2GRAY)
    t = _HT["t"]
    if t is None:
        return None
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    H, W = t.shape[:2]
    y0, y1, x0, x1 = 760, 880, 780, 1460
    win = g[y0:y1, x0:x1]
    if win.shape[0] < H or win.shape[1] < W:
        return None
    r = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(r)
    return (ml[0] + x0 + W // 2, ml[1] + y0 + H // 2, float(mx))


# ---------------------------------------------------------------- 基础
def _beige(im):
    b, g, r = im[:, :, 0].astype(int), im[:, :, 1].astype(int), im[:, :, 2].astype(int)
    return (abs(b - 149) < 45) & (abs(g - 199) < 45) & (abs(r - 211) < 45)


def _white(im):
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 2] > 225) & (hsv[:, :, 1] < 70)).astype(np.uint8)


def _digit_mask(im):
    """价格数字的像素掩码：买得起是白色，买不起是**红色**，两种都要认。

    只认白色会把「钱不够」的面板全判成没有价格（实测：金币只剩 208k 时，
    圣水收集器 2,000,000 金币 / 圣水瓶 2,800,000 金币 这两张面板全部读不出来），
    而这两类恰好是少数几个「花金币」的资源建筑，读不到就没法升。
    """
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    white = (v > 225) & (s < 70)
    red = (v >= 160) & (s >= 80) & ((h <= 12) | (h >= 168))
    return (white | red).astype(np.uint8)


def cards(im, ylo=640, yhi=1000, xlo=500, xhi=1900, min_w=100, col_th=60):
    """底部面板的卡片列 → [{x0,x1,cx,y0,y1}]，x 升序。没有面板返回 []"""
    m = _beige(im)
    band = np.zeros_like(m)
    band[ylo:yhi, xlo:xhi] = True
    m = m & band
    cp = m.sum(axis=0)
    out = []
    i = xlo
    while i < xhi:
        if cp[i] > col_th:
            j = i
            while j < xhi and cp[j] > col_th:
                j += 1
            if j - i >= min_w:
                sub = m[:, i + 20:j - 20]
                yy = np.where(sub.sum(axis=1) > (j - i - 40) * 0.45)[0]
                if len(yy):
                    out.append(dict(x0=i, x1=j - 1, cx=(i + j - 1) // 2,
                                    y0=int(yy.min()), y1=int(yy.max())))
            i = j
        else:
            i += 1
    return out


def _holes(glyph):
    """数字里有几个「洞」：8 有 2 个、0 和 4 有 1 个、其余 0 个。

    为什么需要它：这个字体里 0 和 8 的模板分能打平（实测都是 0.961），
    取 max 时谁在字典里靠前就是谁 → 「8」被读成「0」。
    实测后果：迫击炮（8级）的标题等级读成 0（整张面板被当成点错建筑）、
    圣水瓶 2,800,000（红字）读成 2,000,000。
    """
    m = (np.asarray(glyph) > 0).astype(np.uint8)
    inv = (1 - m).astype(np.uint8)
    n, lab = cv2.connectedComponents(inv, 4)
    border = set(int(v) for v in np.concatenate([lab[0, :], lab[-1, :],
                                                lab[:, 0], lab[:, -1]]))
    holes = 0
    for k in range(1, n):
        if k in border:
            continue
        if int((lab == k).sum()) >= 4:      # 太小的孔是抗锯齿噪点
            holes += 1
    return holes


def _match_digit(glyph, ww, hh, tpl):
    """一位数字的模板匹配 → (数字, 分数)

    先按宽高比拦「1」：这个字体里「1」只有 5-6px 宽（宽高比 0.3-0.4），
    其余数字都在 0.75-1.0。不加这一条会出「0/1 同分 0.96」的平局，
    价格会被读多一位（实测 1,200,000 被读成 1,200,001）。
    """
    ar = ww / float(hh)
    if ar < 0.55:
        v = norm(cv2.resize(glyph, (GW, GH)))
        sc = max(float(np.max(tpl["1"] @ v)), 0.0) if "1" in tpl else 0.0
        return "1", sc
    v = norm(cv2.resize(glyph, (GW, GH)))
    scores = {}
    bd, bst = -2, None
    for d, arrs in tpl.items():
        if d == "1":
            continue
        sc = float(np.max(arrs @ v))
        scores[d] = sc
        if sc > bd:
            bd, bst = sc, d
    # 0 / 8 分接近时按「洞」的个数定：8 两个洞，0 一个洞
    if bst in ("0", "8") and abs(scores.get("0", -2) - scores.get("8", -2)) <= 0.03:
        bst = "8" if _holes(glyph) >= 2 else "0"
        bd = max(scores.get("0", bd), scores.get("8", bd))
    # 0 一定有洞：没洞说明认错了。实测天鹰火炮标题里的「3」和 0 打平（0.951 / 0.947），
    # 取 max 就取成 0，整张面板被判成点错建筑
    if bst == "0" and _holes(glyph) == 0:
        alt = [(sc, d) for d, sc in scores.items() if d != "0"]
        if alt:
            bd, bst = max(alt)
    return bst, bd


def _digit_comps(im, y0, y1, x0, x1):
    """在一段里找「数字」连通域 → [(x,y,w,h,area)]，x 升序（已在全图坐标）

    尺寸是固定 UI：数字 12-17 宽、15-16 高、面积 76-190（「1」只有 6x15/76）。
    所以按 14<=h<=22 + 面积>=60 过滤，能把数字旁边的小碎块（6x11/48）、
    卡片白描边、图标碎片都排掉。
    """
    w = _digit_mask(im)
    sub = w[y0:y1, x0:x1]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sub, 8)
    out = []
    for k in range(1, n):
        x, y, ww, hh, area = stats[k]
        if area < 45:            # 「1」只有 5x14/59，别的碎片都更矮，靠高度筛
            continue
        if not (14 <= hh <= 22 and 4 <= ww <= 24):
            continue
        if hh < ww * 0.75:          # 数字都比宽高；卡片描边/图标是扁的
            continue
        if ww > (x1 - x0) * 0.5:    # 卡片白描边
            continue
        out.append((x + x0, y + y0, int(ww), int(hh), int(area)))
    out.sort()
    return out


def _icon_color(im, x0, x1, y0, y1):
    """价格右边那个资源图标的颜色 → 'gold'/'elixir'/'dark'/'gems'/None

    用「最大色斑的中位色相」判币种（实测：金币 H≈26、圣水洋红 H≈145、绿宝石 H≈50），
    暗黑重油的图标偏暗，用低亮度分支兜底。
    """
    sub = im[max(0, y0):y1, max(0, x0):x1]
    if sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    # 先在窗口里找「最大的一块饱和色斑」当图标。以前直接取整个窗口的中位色相，
    # 窗口偏一点就会落到卡片金边上 → 圣水建筑全被判成 gold（实测 Gold Storage /
    # Laboratory 两张都读成 gold，实际是圣水）。
    m = ((hsv[:, :, 1] >= 110) & (hsv[:, :, 2] >= 110)).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for k in range(1, n):
        bx, by, bw, bh, area = stats[k]
        if area < 40 or bh < 10 or bw < 8:
            continue
        if bw > (x1 - x0) * 0.9:            # 一整条色带（卡片边）不算图标
            continue
        if best is None or area > best[0]:
            best = (area, k)
    if best is not None:
        sel = (lab == best[1])
        h = float(np.median(hsv[:, :, 0][sel]))
        if 10 <= h <= 40:
            return "gold"
        if 40 < h <= 95:
            return "gems"
        if 105 <= h <= 175:
            return "elixir"
        return None
    d = ((hsv[:, :, 2] >= 25) & (hsv[:, :, 2] < 130) & (hsv[:, :, 1] >= 80)).astype(np.uint8)
    if int(d.sum()) >= 60:
        h = float(np.median(hsv[:, :, 0][d > 0]))
        if 110 <= h <= 175:
            return "dark"
    return None


def title_level(im, tpl=None, y0=686, y1=750, x0=700, x1=1560):
    """从面板标题「…（N级）」里读出 N（返回 int 或 None）

    标题里的等级数字是「近白色 + 微黄」（V≥225, S 25-160）、字体比价格大（高 32-34），
    而且笔画实心：填充率 ≥0.45，而汉字只剩描边碎片、填充率 ≤0.27。
    按「面积≥250 + 高≥28 + 填充率≥0.45」筛，就只剩等级数字。
    实测：训练营→14、城墙→12、隐形炸弹→4。

    这一关是「点对了建筑没有」的唯一证据：内存里的等级和面板标题的等级必须一致。
    """
    tpl = tpl if tpl is not None else load_digits()
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 2] >= 225) & (hsv[:, :, 1] >= 25) & (hsv[:, :, 1] <= 160)).astype(np.uint8)
    sub = m[y0:y1, x0:x1]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sub, 8)
    cs = []
    for k in range(1, n):
        x, y, ww, hh, area = stats[k]
        if area < 250 or hh < 28 or ww < 10:
            continue
        if area / float(ww * hh) < 0.45:
            continue
        cs.append((x + x0, y + y0, int(ww), int(hh), int(area), k))
    if not cs:
        return None, ""
    cs.sort()
    txt = ""
    for (x, y, ww, hh, area, k) in cs:
        g = (lab[y - y0:y - y0 + hh, x - x0:x - x0 + ww] == k).astype(np.float32)
        d, sc = _match_digit(g, ww, hh, tpl)
        txt += d if (d and sc > 0.70) else "?"
    return (int(txt) if txt.isdigit() else None), txt


def read(im, tpl=None, dbg=None):
    """识别建筑面板 → dict

    返回 {open, n_cards, cards, up_idx, up_btn, price, currency, digits, group}
      open      底部出现面板卡片
      up_idx    「升级」是第几张卡（x 升序，0 基）
      up_btn    升级按钮点击点 (x, y)；y 取锤子中心（认到锤子时）
      price     面板价格（int）或 None
      currency  'gold'/'elixir'/'dark'/'gems'/None
      price_red 价格是红字（买不起）→ 只当参考值，不当准数
      title_level 标题里的等级（int 或 None）
      group     价格数字+图标那一组的 (x0,x1,y0,y1)，裁剪存图用
    """
    cs = cards(im)
    out = dict(open=bool(cs), n_cards=len(cs), cards=cs, up_idx=None,
               up_btn=None, price=None, currency=None, digits=None, group=None,
               title_band=None, title_level=None, title_digits=None, hammer=None,
               price_red=False)
    if not cs:
        return out
    tpl = tpl if tpl is not None else load_digits()
    lv, lvtxt = title_level(im, tpl)
    out["title_level"] = lv
    out["title_digits"] = lvtxt
    hm = find_hammer(im)
    out["hammer"] = [hm[0], hm[1], round(hm[2], 3)] if hm else None
    hx = hm[0] if (hm and hm[2] >= 0.85) else None
    y1 = max(c["y1"] for c in cs)
    py0, py1 = y1 - 190, y1 - 132          # 价格数字那一行
    out["title_band"] = (max(0, y1 - 275), y1 - 180)

    # 两遍：先按白字（买得起）读，读不到再按红字（买不起）读一次。
    # 白字是第一优先——红字抗锯齿后笔画会糊，实测把「8」读成过「0」
    # （圣水瓶 2,800,000 被读成 2,000,000），所以红字结果只当参考值。
    pb = None
    for mask_fn in (_white, _digit_mask):
        cand = _price_block(im, cs, tpl, hx, py0, py1, mask_fn)
        if pb is None:
            pb = cand
        if cand.get("price") is not None:
            pb = cand
            if mask_fn is _digit_mask:
                pb["price_red"] = True
            break
    out.update(pb)
    if out["up_idx"] is None and hx is not None:
        for k, c in enumerate(cs):
            if c["x0"] - 10 <= hx <= c["x1"] + 10:
                out["up_idx"] = k
                break
    if out["up_idx"] is None:
        out["up_idx"] = 1 if len(cs) >= 2 else 0
    c = cs[out["up_idx"]]
    out["up_btn"] = (int(c["cx"]), int(c["y1"] - 45))
    if hx is not None:
        out["up_btn"] = (int(hx), int(hm[1]))     # 锤子中心 = 按钮中心
    if dbg:
        cv2.imwrite(dbg, im[max(0, py0 - 60):y1 + 10, cs[0]["x0"] - 60:cs[-1]["x1"] + 60])
    return out


def _digit_comps_masked(im, y0, y1, x0, x1, mask):
    """同 _digit_comps，但掩码由调用方给（白字 / 红字两遍用）"""
    sub = mask[y0:y1, x0:x1]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(sub, 8)
    out = []
    for k in range(1, n):
        x, y, ww, hh, area = stats[k]
        if area < 45:
            continue
        if not (14 <= hh <= 22 and 4 <= ww <= 24):
            continue
        if hh < ww * 0.75:
            continue
        if ww > (x1 - x0) * 0.5:
            continue
        out.append((x + x0, y + y0, int(ww), int(hh), int(area)))
    out.sort()
    return out


def _split_merged(comps, mask):
    """把粘连的两个数字拆开（红字尤其容易粘：实测「10 000 000」的 1 和 0 连成一块）。

    只在「这块比同组其他数字宽（≥1.15 倍且 ≥12px）」并且「内部有一列近乎没有像素」
    的时候拆，避免把 0/8 这种本来就胖的数字拆坏。
    """
    if not comps:
        return comps
    med = float(np.median([c[2] for c in comps]))
    out = []
    for (x, y, w, h, area) in comps:
        if w >= max(12.0, med * 1.15):
            sub = (mask[y:y + h, x:x + w] > 0)
            col = sub.sum(axis=0)
            if len(col) > 5:
                i = int(np.argmin(col[2:-2])) + 2
                if col[i] <= 1 and 4 <= i <= w - 4:
                    a, b = sub[:, :i], sub[:, i:]
                    if a.sum() >= 40 and b.sum() >= 40:
                        out.append((x, y, i, h, int(a.sum())))
                        out.append((x + i, y, w - i, h, int(b.sum())))
                        continue
        out.append((x, y, w, h, area))
    out.sort()
    return out


def _price_block(im, cs, tpl, hx, py0, py1, mask_fn):
    """在面板里认「升级」卡上的价格数字 + 币种。mask_fn 决定认白字还是红字。"""
    r = dict(up_idx=None, digits=None, price=None, currency=None, group=None,
             price_red=False)
    mask = mask_fn(im)
    xlo, xhi = cs[0]["x0"] - 60, cs[-1]["x1"] + 60
    comps = _digit_comps_masked(im, py0, py1, xlo, xhi, mask)
    # 按水平间隔聚簇（同一串数字间隔 ≤26px）
    # 同一串数字必须在同一条基线上：JPEG 压缩会在卡片描边处冒出 9x17 的假块
    # （顶边比真数字高 12px），不按基线过滤会把 2,000,000 读成 12,000,000
    comps = _split_merged(comps, mask)
    groups = []
    for c in comps:
        if groups and c[0] - (groups[-1][-1][0] + groups[-1][-1][2]) <= 26:
            groups[-1].append(c)
        else:
            groups.append([c])
    # 再按「卡片横向范围」补一组：价格牌压在卡片正上方，而 "10 000 000" 这种
    # 千位之间有空格的会被上面的 26px 规则拆成两组（实测组间 40px，只剩「00000」）
    for c in cs:
        lo = c["cx"] - (c["x1"] - c["x0"]) // 2 - 40
        hi = c["cx"] + (c["x1"] - c["x0"]) // 2 + 40
        gp = [t for t in comps if lo <= t[0] <= hi]
        if len(gp) >= 2:
            groups.append(gp)
    best = None
    for gp in groups:
        if len(gp) < 2:
            continue
        my = sorted(c[1] for c in gp)[len(gp) // 2]
        mh = sorted(c[3] for c in gp)[len(gp) // 2]
        # 基线容忍：天鹰火炮那张面板的红字「10」比后面的 0 低 6px 且高 4px（
        # 1 和 0 粘在一起、下沿还带了卡片装饰），卡 ±5 会把整个 10 丢掉
        gp = [c for c in gp if abs(c[1] - my) <= 8
              and abs((c[1] + c[3]) - (my + mh)) <= 10]
        if len(gp) < 2:
            continue
        gx0 = gp[0][0]
        gx1 = gp[-1][0] + gp[-1][2]
        gcx = (gx0 + gx1) / 2
        # 价格牌一定压在「升级」卡上：优先和锤子对齐的那一串；
        # 锤子没认出来时退回「和某张卡中心对齐」的旧规则
        for k, c in enumerate(cs):
            if hx is not None:
                if abs(gcx - hx) > 60:
                    continue
                score = 100 - abs(gcx - hx)
            elif abs(gcx - c["cx"]) <= 55:
                score = len(gp)
            else:
                continue
            if best is None or score > best[0]:
                best = (score, k, gp, gx0, gx1)
    if best is None:
        return r
    _s, k, gp, gx0, gx1 = best
    r["up_idx"] = k
    txt = ""
    for (x, y, ww, hh, area) in gp:
        g = np.zeros((hh, ww), np.float32)
        g[mask[y:y + hh, x:x + ww] > 0] = 1.0
        d, sc = _match_digit(g, ww, hh, tpl)
        txt += d if (d and sc > 0.70) else "?"
    r["digits"] = txt
    # 价格不可能以 0 开头、也不可能小于 100：Army Camp 的 7,000,000 就被读成过
    # 「0」(前导 7 缩水成 10x16 被模板判成了 0)，宁可记成未知也别记成 0
    if txt.isdigit() and len(txt) <= 12 and not txt.startswith("0") and int(txt) >= 100:
        r["price"] = int(txt)
    r["currency"] = _icon_color(im, gx1 + 1, min(gx1 + 60, im.shape[1] - 1), py0, py1)
    r["group"] = (int(gx0), int(gx1), int(py0), int(py1))
    # 图标也可能在这一串的左边（金币在右侧，一律右侧找；找不到再左侧）
    if r["currency"] in (None, "?"):
        r["currency"] = _icon_color(im, max(0, gx0 - 60), gx0 - 1, py0, py1)
    return r


# ---------------------------------------------------------------- 确认页 / 时长
# 确认页右下角「升级时间 / 1天12小时」那一块（19.100.1 实测：标签 y≈900、值 y≈950）
DUR_REGIONS = ((1500, 880, 1990, 1000), (1440, 850, 2060, 1030))


def read_duration(im, regions=None):
    """确认页上的「升级时间 2天1小时53分钟」→ {text, seconds, region}

    只读那一小块、只认「能整串解析成时长」的框：OCR 会把旁边按钮上的价格数字
    （「1 800 000」）也吐出来，那些解析不出单位，就被 parse_duration 挡掉了。
    解析不出来就返回 seconds=None，不猜（页面上宁可显示「时长未知」）。
    """
    import coc_duration
    out = dict(text=None, seconds=None, region=None, tried=[])
    if im is None:
        return out
    import coc_patrol2
    for region in (regions or DUR_REGIONS):
        x0, y0, x1, y1 = region
        x1, y1 = min(x1, im.shape[1]), min(y1, im.shape[0])
        try:
            boxes = coc_patrol2.ocr_all(im, (x0, y0, x1, y1))
        except Exception:
            boxes = []
        out["tried"].append([t.get("t") for t in boxes])
        for b in boxes:
            sec = coc_duration.parse_duration(b.get("t"))
            if sec:
                out.update(text=b["t"], seconds=sec, region=list(region))
                return out
    return out


# ---------------------------------------------------------------- 确认页
def confirm_page(im):
    """识别「升级确认页」→ {open, btn (x,y) or None, btn_box, close (x,y)}

    确认页是整屏模态：中间一大块浅灰卡片 + 右下绿色「确认」按钮。
    绿色按钮本身也当作判据：按钮变灰（资源不够/不可升）时不给点击点。
    """
    b = im[:, :, 0].astype(int)
    g = im[:, :, 1].astype(int)
    r = im[:, :, 2].astype(int)
    grn = ((g > 175) & (r < 215) & (b < 150) & (g > b + 60) & (g > r + 30))
    band = np.zeros_like(grn)
    band[840:1010, 1250:1800] = True
    m = (grn & band).astype(np.uint8)
    n = int(m.sum())
    out = dict(open=False, btn=None, btn_box=None, n_green=n, gray_frac=0.0,
               close=(1926, 76))
    if n < 2000:
        return out
    ys, xs = np.where(m)
    out["btn_box"] = (int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max()))
    out["btn"] = (int((xs.min() + xs.max()) / 2), int((ys.min() + ys.max()) / 2))
    # 中间浅灰卡片：确认页把村庄盖住了，右上一大片低饱和浅灰
    # 实测确认页 0.75 / 村庄 0.09
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    mid = hsv[200:760, 1200:1850]
    grayish = ((mid[:, :, 1] < 50) & (mid[:, :, 2] > 150))
    out["gray_frac"] = round(float(grayish.mean()), 3)
    out["open"] = n >= 6000 and out["gray_frac"] > 0.35
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        im = cv2.imread(p)
        if im is None:
            print(p, "读不出来")
            continue
        print(p, "duration:", read_duration(im))
        r = read(im)
        r.pop("cards", None)
        print(p, r)
        print("   confirm:", confirm_page(im))
