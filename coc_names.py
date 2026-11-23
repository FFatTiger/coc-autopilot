#!/usr/bin/env python3
"""游戏里的英文名 → 中文名（国服叫法）。

为什么单独一个模块
  看板的读者不看英文。类型名来自内存（`building_types.json` 里的英文名）和历史记录
  （「Dark Elixir Storage(6→7)」这种），渲染前统一在这里换掉，页面里就不需要再拼字符串。
  这里只做「显示」——键名、状态机、判断逻辑一律还是英文，调用方拿原值用。

用法：
  python3 coc_names.py                    # 打一遍映射表，顺便检查覆盖
  python3 coc_names.py --check            # 只检查 building_types.json 有没有漏
  from coc_names import zh, zh_type       # zh("Cannon(14→15)") → "加农炮(14→15)"
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------- 建筑 ----------------
# 键与 building_types.json / 内存里 type 字段完全一致（大小写敏感），值是国服叫法。
BUILDING = {
    # 主村 · 资源与功能建筑
    "Town Hall": "大本营",
    "Gold Mine": "金矿",
    "Elixir Collector": "圣水收集器",
    "Gold Storage": "金币仓库",
    "Elixir Storage": "圣水仓库",
    "Dark Elixir Storage": "暗黑重油仓库",
    "Dark Elixir Drill": "暗黑重油钻井",
    "Barracks": "兵营",
    "Dark Barracks": "暗黑兵营",
    "Army Camp": "军营",
    "Laboratory": "实验室",
    "Spell Factory": "法术工厂",
    "Dark Spell Factory": "暗黑法术工厂",
    "Clan Castle": "部落城堡",
    "Builders Hut": "建筑工人小屋",
    "Builder's Hut": "建筑工人小屋",
    "Helper Hut": "帮手小屋",
    "Blacksmith": "铁匠铺",
    "Pet House": "宠物屋",
    "Hero Hall": "英雄大厅",
    "Crafting Station": "制作工坊",
    "Siege Workshop": "攻城机器工坊",
    "Workshop": "攻城机器工坊",
    # 主村 · 防御
    "Cannon": "加农炮",
    "Archer Tower": "箭塔",
    "Mortar": "迫击炮",
    "Air Defense": "防空火箭",
    "Wizard Tower": "法师塔",
    "Hidden Tesla": "隐形特斯拉",
    "Tesla": "隐形特斯拉",
    "Bomb Tower": "炸弹塔",
    "X-Bow": "X连弩",
    "Inferno Tower": "地狱塔",
    "Eagle Artillery": "鹰火炮",
    "Air Sweeper": "空气炮",
    "Monolith": "巨石碑",
    "Ricochet Cannon": "弹射加农炮",
    "Multi-Archer Tower": "多重箭塔",
    "Multi-Gear Tower": "多重齿轮塔",
    "Firespitter": "喷火器",
    "Wall": "城墙",
    "Walls": "城墙",
    # 主村 · 陷阱
    "Bomb": "炸弹",
    "Giant Bomb": "巨型炸弹",
    "Air Bomb": "空中炸弹",
    "Seeking Air Mine": "追踪空中地雷",
    "Spring Trap": "弹簧陷阱",
    "Skeleton Trap": "骷髅陷阱",
    "Tornado Trap": "龙卷风陷阱",
    "Mine": "地雷",
    "Mega Mine": "巨型地雷",
    "Mega Tesla": "巨型特斯拉",
    # 夜世界
    "BB Builder Hall": "建筑大师大本营",
    "BB Wall": "建筑大师城墙",
    "BB Cannon": "加农炮·夜世界",
    "BB Double Cannon": "双管加农炮",
    "BB Archer Tower": "箭塔·夜世界",
    "BB Hidden Tesla": "隐形特斯拉·夜世界",
    "BB Multi Mortar": "多管迫击炮",
    "BB Crusher": "碎石机",
    "BB Air Bombs": "空中炸弹发射器",
    "BB Firecrackers": "焰火发射器",
    "BB Roaster": "熔岩喷射器",
    "BB Guard Post": "哨卫营地",
    "BB Clock Tower": "钟楼",
    "BB Star Laboratory": "明星实验室",
    "BB Gem Mine": "宝石矿",
    "BB Gold Mine": "金矿·夜世界",
    "BB Elixir Collector": "圣水收集器·夜世界",
    "BB Gold Storage": "金币仓库·夜世界",
    "BB Elixir Storage": "圣水仓库·夜世界",
    "BB Barracks": "兵营·夜世界",
    "BB Army Camp": "军营·夜世界",
    "BB Battle Machine Altar": "战斗机器祭坛",
    "BB OTTOs Outpost": "OTTO 哨站",
    "BB Reinforcement Camp": "援兵营地",
}

# ---------------- 兵种 ----------------
UNIT = {
    "Barbarian": "野蛮人",
    "Archer": "弓箭手",
    "Giant": "巨人",
    "Goblin": "哥布林",
    "Wall Breaker": "炸弹人",
    "Balloon": "气球兵",
    "Wizard": "法师",
    "Healer": "天使",
    "Dragon": "飞龙",
    "P.E.K.K.A": "皮卡超人",
    "Baby Dragon": "飞龙宝宝",
    "Minion": "亡灵",
    "Hog Rider": "野猪骑士",
    "Valkyrie": "瓦基丽武神",
    "Golem": "戈仑石人",
    "Witch": "女巫",
    "Lava Hound": "熔岩猎犬",
    "Bowler": "巨石投手",
    "Miner": "掘地矿工",
    "Electro Dragon": "雷电飞龙",
    "Yeti": "雪怪",
    "Dragon Rider": "飞龙骑士",
    "Electro Titan": "雷电泰坦",
    "Root Rider": "根蔓骑士",
    "Ice Golem": "戈仑冰人",
    "Headhunter": "英雄猎手",
    "Apprentice Warden": "学徒守护者",
    "Super Barbarian": "超级野蛮人",
    "Super Archer": "超级弓箭手",
    "Sneaky Goblin": "鬼祟哥布林",
    "Super Wall Breaker": "超级炸弹人",
    "Super Giant": "超级巨人",
    "Rocket Balloon": "火箭气球兵",
    "Inferno Dragon": "地狱飞龙",
    "Super Minion": "超级亡灵",
    "Super Valkyrie": "超级瓦基丽",
    "Super Witch": "超级女巫",
    "Ice Hound": "寒冰猎犬",
    "Super Bowler": "超级巨石投手",
    "Super Dragon": "超级飞龙",
    "Super Miner": "超级掘地矿工",
    "Wall Wrecker": "攻城战车",
    "Battle Blimp": "攻城飞艇",
    "Stone Slammer": "攻城锤",
    "Siege Barracks": "攻城兵营",
    "Log Launcher": "滚木发射器",
    "Flame Flinger": "喷火投射器",
    "Battle Drill": "攻城钻机",
    "Troop Launcher": "兵种发射器",
    "Minion Prince": "亡灵王子",
    "Barbarian King": "野蛮人之王",
    "Archer Queen": "弓箭女皇",
    "Grand Warden": "大守护者",
    "Royal Champion": "皇家冠军",
    "Battle Machine": "战斗机器",
}

# ---------------- 法术 ----------------
SPELL = {
    "Lightning": "雷电法术",
    "Heal": "治疗法术",
    "Healing": "治疗法术",
    "Rage": "狂暴法术",
    "Jump": "弹跳法术",
    "Freeze": "冰冻法术",
    "Clone": "镜像法术",
    "Invisibility": "隐形法术",
    "Recall": "召回法术",
    "Poison": "毒药法术",
    "Earthquake": "地震法术",
    "Haste": "急速法术",
    "Skeleton": "骷髅法术",
    "Bat": "蝙蝠法术",
    "Overgrowth": "蔓生法术",
    "Revive": "复活法术",
    "Totem": "图腾法术",
    "Ice Block": "冰封法术",
}

# ---------------- 界面用词 ----------------
STATUS = {
    "ok": "正常",
    "timeout": "超时",
    "failed": "失败",
    "error": "出错",
    "interrupted": "中断",
    "running": "运行中",
    "done": "已完成",
    "expired": "状态失效",
    "in_progress": "施工中",
    "estimated": "时间近似",
    "idle": "空闲",
    "busy": "施工中",
    "total": "总数",
    "unknown": "未知",
    "worker": "工人",
    "research": "研究",
}

ALL = {}
ALL.update(UNIT)
ALL.update(SPELL)
ALL.update(BUILDING)

# 一个中文兜底：实在没映射到，也不能把英文原样丢给用户
FALLBACK = {"building": "未知建筑", "unit": "未知兵种", "spell": "未知法术",
            "any": "未知项目"}

_CJK = re.compile(r"[\u4e00-\u9fff]")
# 「Cannon(14→15)」「Dark Elixir Storage(6→7)」→ 主体 + 括号尾巴
_TAIL = re.compile(r"^(.*?)\s*[（(]\s*([^()（）]*)\s*[)）]\s*$")
# 「圣水收集器×5」「Dark Elixir DrillX5」→ 主体 + 数量
_MULT = re.compile(r"^(.*?)\s*[×xX]\s*(\d+)$")


def _lookup(name, table=None):
    """精确 → 忽略大小写/多余空格 → 单复数 → None"""
    table = table or ALL
    if name in table:
        return table[name]
    key = " ".join(str(name).split())
    if key in table:
        return table[key]
    low = {k.lower(): v for k, v in table.items()}
    if key.lower() in low:
        return low[key.lower()]
    for suffix in ("s", "es"):
        if key.lower().endswith(suffix):
            k2 = key[: -len(suffix)].lower()
            if k2 in low:
                return low[k2]
    return None


def zh(name, kind="any", keep_en=False):
    """英文名 → 中文名（带括号/数量后缀的原样保留）。

    zh("Cannon(14→15)")      → 加农炮(14→15)
    zh("Dark Elixir DrillX5")→ 暗黑重油钻井×5
    zh("野蛮人")             → 野蛮人（本来就是中文，原样返回）
    zh("NewFangledThing")    → 未知建筑（英文不外露；keep_en 时带括号原名）
    """
    if name is None:
        return FALLBACK.get(kind, FALLBACK["any"])
    s = str(name).strip()
    if not s:
        return FALLBACK.get(kind, FALLBACK["any"])
    if _CJK.search(s):
        # 已经是中文（含「野蛮人（已过 0.4/49.0 小时）」这类），只把 x5 规范成 ×5
        m = _MULT.match(s)
        return (m.group(1) + "×" + m.group(2)) if m else s

    m = _TAIL.match(s)
    if m and _lookup(m.group(1)):
        return _lookup(m.group(1)) + "(" + m.group(2) + ")"

    m = _MULT.match(s)
    if m and _lookup(m.group(1)):
        return _lookup(m.group(1)) + "×" + m.group(2)

    if "|" in s:                       # "Cannon|14|88.0,34.0" 这种内部 key
        head, rest = s.split("|", 1)
        got = _lookup(head)
        return (got + "|" + rest) if got else FALLBACK.get(kind, FALLBACK["any"])

    hit = _lookup(s)
    if hit:
        return hit
    if kind == "status":
        return STATUS.get(s.lower(), FALLBACK["any"])
    base = FALLBACK.get(kind, FALLBACK["any"])
    return base + ("（" + s + "）" if keep_en else "")


def _bare(s):
    """去掉「(14→15)」「×5」这种尾巴，拿主体"""
    s = str(s or "").strip()
    m = _TAIL.match(s) or _MULT.match(s)
    return (m.group(1) if m else s).strip()


def en_name(name):
    """中文名 → 英文名（图标文件名要用英文拼）；本来英文且认识的原样返回"""
    s = _bare(name)
    if not _CJK.search(s):
        return s if _lookup(s) else None
    for table in (UNIT, SPELL, BUILDING):
        for en, zhv in table.items():
            if zhv == s:
                return en
    return None


def icon_key(name):
    """名字（中文或英文）→ static/icons 里的文件名（不含 .png），认不出给 None"""
    en = en_name(name)
    return ("b_" + en.replace(" ", "_")) if en else None


def localize_state(p):
    """把 /api/state 的展示字段换成中文。

    键名、状态机字段一律不动（前端还要按 kind/currency/进度算），
    只加 type_en / icon 两个给图标用的字段，并把 type/name/label 换成中文。
    """
    if not isinstance(p, dict):
        return p
    for it in p.get("items") or []:
        if not isinstance(it, dict):
            continue
        en = it.get("type") or it.get("name")
        it["type_en"] = en
        it["icon"] = icon_key(en) or icon_key(it.get("name"))
        if it.get("kind") == "research":
            it["name"] = zh(it.get("name"), "unit")
        else:
            it["type"] = zh_type(it.get("type"))[0]
            it["name"] = zh(it.get("name"), "building")
        it["label"] = zh(it.get("label"), "building")
    for d in p.get("done") or []:
        if not isinstance(d, dict):
            continue
        en = d.get("type") or d.get("name")
        d["type_en"] = en
        d["icon"] = icon_key(en) or icon_key(d.get("name"))
        d["name"] = zh(d.get("name"), "building")
        d["type"] = zh(d.get("type"), "building")
    p["unmapped"] = unknown_types()
    return p


def localize_run(d):
    """历史记录的一条：把建筑/兵种名换成中文（状态仍是英文键，前端配色用）"""
    if not isinstance(d, dict):
        return d
    for i in d.get("items") or []:
        if isinstance(i, dict) and i.get("name"):
            i["name"] = zh(i["name"], "building")
    up = d.get("upgrade")
    if isinstance(up, dict):
        if up.get("name"):
            up["name"] = zh(up["name"], "building")
        up["icon"] = icon_key(up.get("name"))
    rsc = d.get("research")
    if isinstance(rsc, dict):
        for key in ("busy", "picked"):
            pass
        picked = rsc.get("picked")
        if isinstance(picked, dict):
            picked["icon"] = icon_key(picked.get("name"))
        res = rsc.get("result")
        if isinstance(res, dict):
            res["icon"] = icon_key(res.get("name"))
    return d


def zh_type(building_type):
    """建筑类型专用：返回 (中文名, 是否有映射)"""
    s = str(building_type or "").strip()
    hit = _lookup(s, BUILDING)
    return (hit, True) if hit else (FALLBACK["building"], False)


def unknown_types(path=None):
    """building_types.json 里还没映射的类型（自检用）"""
    path = path or os.path.join(HERE, "building_types.json")
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return []
    miss = []
    for meta in (d.get("types") or {}).values():
        n = (meta or {}).get("name")
        if n and not _lookup(n, BUILDING):
            miss.append(n)
    return sorted(set(miss))


def main():
    if "--check" in sys.argv:
        miss = unknown_types()
        print("未映射类型:", miss if miss else "无")
        return 0 if not miss else 1
    print(f"建筑 {len(BUILDING)} · 兵种 {len(UNIT)} · 法术 {len(SPELL)}")
    for k, v in list(BUILDING.items())[:6]:
        print(f"  {k:26s} → {v}")
    print("样例:",
          zh("Dark Elixir Storage(6→7)"), "|", zh("Gold Mine(14→15)"),
          "|", zh("野蛮人x5"), "|", zh("SomethingNew", kind="building"),
          "|", zh("timeout", kind="status"))
    miss = unknown_types()
    print("building_types.json 未映射:", miss if miss else "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
