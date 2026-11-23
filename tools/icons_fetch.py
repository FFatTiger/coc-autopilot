#!/usr/bin/env python3
"""从社区 wiki 下载建筑/兵种/法术立绘 → static/icons/*.png（64×64、透明底、≤6KB）。

为什么走这条路：APK 里建筑美术是 scattachment 打包的 3D 资源（没有按名字命名的 PNG），
解包成本高；wiki 上每个建筑每一级都有一张透明底立体图，直接拿最省事，也是自用的看板。

用法：
  python3 tools/icons_fetch.py            # 全部
  python3 tools/icons_fetch.py --only Cannon "Wizard Tower"
  python3 tools/icons_fetch.py --sheet    # 另外拼一张总览图方便肉眼验收
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "static", "icons")
API = "https://clashofclans.fandom.com/api.php"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) coc-dashboard/1.0"}
SIZE = 80          # 显示 32~40，2x 屏
MAXB = 6144        # 单张上限 6KB

# 类型 → wiki 文件名前缀（多个候选，按顺序试）
PREFIX = {
    # 村庄
    "Town Hall": ["Town Hall"],
    "Gold Mine": ["Gold Mine"],
    "Elixir Collector": ["Elixir Collector"],
    "Gold Storage": ["Gold Storage"],
    "Elixir Storage": ["Elixir Storage"],
    "Dark Elixir Storage": ["Dark Elixir Storage"],
    "Dark Elixir Drill": ["Dark Elixir Drill"],
    "Barracks": ["Barracks"],
    "Dark Barracks": ["Dark Barracks"],
    "Army Camp": ["Army Camp"],
    "Laboratory": ["Laboratory"],
    "Spell Factory": ["Spell Factory"],
    "Dark Spell Factory": ["Dark Spell Factory"],
    "Clan Castle": ["Clan Castle"],
    "Builders Hut": ["Builders Hut"],
    "Helper Hut": ["Helper Hut", "Helper's Hut"],
    "Builder's Hut": ["Builders Hut"],
    "Cannon": ["Cannon"],
    "Archer Tower": ["Archer Tower"],
    "Mortar": ["Mortar"],
    "Air Defense": ["Air Defense", "Air Defence"],
    "Wizard Tower": ["Wizard Tower"],
    "Hidden Tesla": ["Hidden Tesla"],
    "Tesla": ["Hidden Tesla"],
    "Bomb Tower": ["Bomb Tower"],
    "X-Bow": ["X-Bow"],
    "Inferno Tower": ["Inferno Tower"],
    "Eagle Artillery": ["Eagle Artillery"],
    "Air Sweeper": ["Air Sweeper"],
    "Siege Workshop": ["Siege Workshop"],
    "Workshop": ["Siege Workshop"],
    "Blacksmith": ["Blacksmith"],
    "Pet House": ["Pet House"],
    "Hero Hall": ["Hero Hall"],
    "Crafting Station": ["Crafting Station"],
    "Monolith": ["Monolith"],
    "Ricochet Cannon": ["Ricochet Cannon"],
    "Multi-Archer Tower": ["Multi-Archer Tower"],
    "Multi-Gear Tower": ["Multi-Gear Tower"],
    "Firespitter": ["Firespitter"],
    "Wall": ["Wall"],
    "Walls": ["Wall"],
    # 陷阱
    "Giant Bomb": ["Giant Bomb"],
    "Bomb": ["Bomb"],
    "Air Bomb": ["Air Bomb"],
    "Seeking Air Mine": ["Seeking Air Mine"],
    "Spring Trap": ["Spring Trap"],
    "Skeleton Trap": ["Skeleton Trap"],
    "Tornado Trap": ["Tornado Trap"],
    "Mega Mine": ["Mega Mine"],
    "Mine": ["Mine"],
    # 夜世界
    "BB Builder Hall": ["Builder Hall"],
    "BB Double Cannon": ["Double Cannon"],
    "BB Multi Mortar": ["Multi Mortar"],
    "BB Crusher": ["Crusher"],
    "BB Air Bombs": ["Air Bombs"],
    "BB Firecrackers": ["Firecrackers"],
    "BB Roaster": ["Roaster"],
    "BB Guard Post": ["Guard Post"],
    "BB Clock Tower": ["Clock Tower"],
    "BB Star Laboratory": ["Star Laboratory"],
    "BB Gem Mine": ["Gem Mine"],
    "BB Battle Machine Altar": ["Battle Machine"],
    "BB OTTOs Outpost": ["OTTO's Outpost", "OTTOs Outpost"],
    "BB Reinforcement Camp": ["Reinforcement Camp"],
    # 兵种
    "Barbarian": ["Barbarian"], "Archer": ["Archer"], "Giant": ["Giant"],
    "Wall Breaker": ["Wall Breaker"], "Goblin": ["Goblin"], "Balloon": ["Balloon"],
    "Wizard": ["Wizard"], "Healer": ["Healer"], "Dragon": ["Dragon"],
    "P.E.K.K.A": ["P.E.K.K.A"], "Baby Dragon": ["Baby Dragon"], "Minion": ["Minion"],
    "Hog Rider": ["Hog Rider"], "Valkyrie": ["Valkyrie"], "Golem": ["Golem"],
    "Witch": ["Witch"], "Lava Hound": ["Lava Hound"], "Bowler": ["Bowler"],
    "Miner": ["Miner"], "Electro Dragon": ["Electro Dragon"], "Yeti": ["Yeti"],
    "Dragon Rider": ["Dragon Rider"], "Electro Titan": ["Electro Titan"],
    "Root Rider": ["Root Rider"], "Ice Golem": ["Ice Golem"], "Headhunter": ["Headhunter"],
    "Apprentice Warden": ["Apprentice Warden"],
    # 法术
    "Lightning": ["Lightning Spell"], "Heal": ["Healing Spell"],
    "Rage": ["Rage Spell"], "Jump": ["Jump Spell"], "Freeze": ["Freeze Spell"],
    "Clone": ["Clone Spell"], "Invisibility": ["Invisibility Spell"],
    "Recall": ["Recall Spell"], "Poison": ["Poison Spell"],
    "Earthquake": ["Earthquake Spell"], "Haste": ["Haste Spell"],
    "Skeleton": ["Skeleton Spell"], "Bat": ["Bat Spell"], "Overgrowth": ["Overgrowth Spell"],
}


def api(params):
    q = dict(params, format="json")
    url = API + "?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def download(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def page_image(title):
    """退路：页面里最像「图标」的那张图。

    优先级：{名字} info.png / Avatar {名字}.png / {名字}.png —— 这三种都是官方立绘或图标，
    页面首图（pageimages）常是活动宣传图，所以不用它。
    """
    d = api({"action": "query", "titles": title, "prop": "images", "imlimit": 500})
    names = []
    for pg in ((d.get("query") or {}).get("pages") or {}).values():
        names = [i["title"][5:] for i in pg.get("images", [])]
    if not names:
        return None
    n = re.escape(title)
    pats = [r"^" + n + r"[ _]info\.png$",
            r"^Avatar[ _]" + n + r"\.png$",
            r"^" + n + r"\.png$",
            r"^" + n + r"[ _]Icon\.png$"]
    for pat in pats:
        for cand in names:
            if re.match(pat, cand, re.I):
                info = api({"action": "query", "titles": "File:" + cand,
                            "prop": "imageinfo", "iiprop": "url"})
                for pg in ((info.get("query") or {}).get("pages") or {}).values():
                    ii = pg.get("imageinfo")
                    if ii:
                        return ii[0]["url"], cand
    return None


def list_files(prefix):
    """allimages 前缀查询（含后续分页），返回 [{name,url,width,height}]"""
    out, cont = [], {}
    for _ in range(6):
        p = {"action": "query", "list": "allimages", "aiprefix": prefix,
             "ailimit": 500, "aiprop": "url|size"}
        p.update(cont)
        d = api(p)
        rows = ((d.get("query") or {}).get("allimages")) or []
        out += rows
        cont = d.get("continue") or {}
        if not cont or not rows:
            break
    return out


def pick(files, name):
    """从同名系列里挑最合适的一张：等级最高的，优先无后缀（其次 Multi/Single/Ground/Air）"""
    p = name.replace(" ", "_")
    pat = re.compile(r"^" + re.escape(p) + r"_?(\d+)?(_(Multi|Single|Ground|Air))?\.png$", re.I)
    best = None
    for f in files:
        m = pat.match(f["name"])
        if not m:
            continue
        raw = m.group(1) or ""
        # 只认 1~2 位的等级：Barbarian117.png / Building23 这类专题图不是立绘
        lvl = int(raw) if raw and len(raw) <= 2 else 0
        var = (m.group(3) or "").lower()
        w, h = f.get("width") or 0, f.get("height") or 0
        ar = (w / h) if h else 0
        key = (
            lvl,
            1 if 120 <= max(w, h) <= 1700 else 0,     # 太小糊、太大是宣传图
            1 if 0.45 <= ar <= 2.2 else 0,            # 立绘基本是竖构图
            {"": 3, "multi": 2, "ground": 2, "single": 1, "air": 1}.get(var, 0),
        )
        if not best or key > best[0]:
            best = (key, f)
    if best:
        return best[1]
    # 退路：{prefix}.png（无等级）
    for f in files:
        if f["name"].lower() == p.lower() + ".png":
            return f
    return None


def cut_alpha(im):
    """没透明底的：从四角泛洪，把连通的近似背景色抠掉"""
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    a = im.getchannel("A")
    if a.getextrema()[0] < 250:      # 已经有真透明
        return im
    from collections import deque
    px = im.load()
    w, h = im.size
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    bg = max(set(corners), key=corners.count)[:3]
    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        q.append((x, 0)); q.append((x, h - 1))
    for y in range(h):
        q.append((0, y)); q.append((w - 1, y))
    tol = 26
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        seen[i] = 1
        r, g, b, al = px[x, y]
        if al == 0 or (abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])) > tol * 3:
            continue
        px[x, y] = (r, g, b, 0)
        if x > 0: q.append((x - 1, y))
        if x < w - 1: q.append((x + 1, y))
        if y > 0: q.append((x, y - 1))
        if y < h - 1: q.append((x, y + 1))
    return im


def square_thumb(im):
    """裁到内容边界 → 补成正方形 → 缩到 SIZE"""
    im = cut_alpha(im)
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    s = max(w, h)
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    canvas.paste(im, ((s - w) // 2, (s - h) // 2))
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def encode(im):
    """≤6KB：调色板量化，颜色从多到少试；还不行就缩一级再试"""
    for size in (im.size[0], max(48, im.size[0] * 3 // 4)):
        cur = im if size == im.size[0] else im.resize((size, size), Image.LANCZOS)
        for colors in (192, 160, 128, 96, 72, 48, 32):
            buf = io.BytesIO()
            cur.quantize(colors=colors, method=Image.FASTOCTREE).save(
                buf, format="PNG", optimize=True)
            if buf.tell() <= MAXB:
                return buf.getvalue()
    return buf.getvalue()


RESOURCES = {"gold": "Gold", "elixir": "Elixir", "dark": "Dark Elixir", "gems": "Gem"}


def fetch_one(key, prefixes, verbose=True, out_name=None):
    for pref in prefixes:
        try:
            files = list_files(pref)
        except Exception as e:
            print(f"  ! {key}: 列表失败 {e}"); continue
        f = pick(files, pref)
        if not f:
            continue
        try:
            im = Image.open(io.BytesIO(download(f["url"])))
            thumb = square_thumb(im)
            data = encode(thumb)
        except Exception as e:
            print(f"  ! {key}: 下载/处理失败 {e}"); continue
        name = out_name or ("b_" + key.replace(" ", "_") + ".png")
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(data)
        if verbose:
            print(f"  ✓ {key:24s} ← {f['name']:34s} {f['width']}×{f['height']} "
                  f"→ {len(data)/1024:.1f}KB")
        return True
    # 系列图找不到 → 用页面首图
    for pref in prefixes:
        found = page_image(pref)  # noqa: E501
        if not found:
            continue
        url, fname = found
        try:
            im = Image.open(io.BytesIO(download(url)))
            data = encode(square_thumb(im))
        except Exception as e:
            print(f"  ! {key}: 首图处理失败 {e}"); continue
        with open(os.path.join(OUT, out_name or ("b_" + key.replace(" ", "_") + ".png")), "wb") as fh:
            fh.write(data)
        if verbose:
            print(f"  ✓ {key:24s} ← {fname:34s} → {len(data)/1024:.1f}KB")
        return True
    print(f"  ✗ {key}: 没找到合适的图")
    return False


def sheet(keys):
    """总览：方便一眼看出哪张图标不对"""
    imgs = []
    for k in keys:
        p = os.path.join(OUT, "b_" + k.replace(" ", "_") + ".png")
        if os.path.exists(p):
            imgs.append((k, Image.open(p).convert("RGBA")))
    if not imgs:
        return
    cols, cell, pad = 8, SIZE, 10
    rows = (len(imgs) + cols - 1) // cols
    from PIL import ImageDraw
    canvas = Image.new("RGBA", (cols * (cell + pad) + pad, rows * (cell + pad + 14) + pad),
                       (245, 246, 248, 255))
    d = ImageDraw.Draw(canvas)
    for i, (k, im) in enumerate(imgs):
        x = pad + (i % cols) * (cell + pad)
        y = pad + (i // cols) * (cell + pad + 14)
        canvas.alpha_composite(im, (x, y))
        d.text((x, y + cell + 1), k[:12], fill=(40, 44, 50, 255))
    out = os.environ.get("ICONS_SHEET") or "/tmp/icons_sheet.png"
    canvas.save(out)
    print("总览图:", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--resources", action="store_true", help="只做金币/圣水/暗黑/宝石四个")
    ap.add_argument("--sheet", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if a.resources:
        for key, page in RESOURCES.items():
            fetch_one(key, [page], out_name=key + ".png")
        return
    keys = a.only or list(PREFIX)
    ok = 0
    for k in keys:
        if k not in PREFIX:
            print(f"  ? {k}: 没有配置 wiki 前缀"); continue
        ok += bool(fetch_one(k, PREFIX[k]))
        time.sleep(0.25)
    print(f"完成 {ok}/{len(keys)}")
    if a.sheet:
        sheet([k for k in keys if k in PREFIX])


if __name__ == "__main__":
    main()
