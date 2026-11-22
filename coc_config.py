#!/usr/bin/env python3
"""巡检配置读写：单一 JSON 文件，看板和巡检脚本共用。

时区处理：cron 每小时触发一次，脚本自己按北京时间判断"到点没"，
这样改配置不需要动 crontab，也不受美国夏令时切换影响。
"""
import json
import os
import sys
import tempfile

CONFIG_PATH = os.environ.get("COC_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "backend": "ocr",           # 读数和判断的来源：ocr = 截图 + OCR，memory = /proc/<pid>/mem
    "schedule": ["09:00", "13:00", "18:00", "22:00"],   # 北京时间，HH:MM（只对 OCR 后端生效）
    "collect": True,            # 是否收资源
    "upgrade": True,            # 是否自动升级
    # 允许消耗的资源类型。默认只开前两种是给 OCR 后端留的保守值；
    # 内存后端按需在 config.json 里写成 ["gold","elixir","dark"]（见 config.example.json）
    "upgrade_kinds": ["gold", "elixir"],
    "max_price": 3_000_000,     # 单次升级价格上限（老键：跨币种一刀切；memory 后端用下面的分币种上限）
    "max_upgrades": 1,          # OCR 后端：每轮最多升几个
    "min_free_workers": 1,      # OCR 后端：至少留几个空闲工人（1 = 有工人就升）

    # 下面这些只对 memory 后端生效（coc_mem_auto）
    "max_targets": 5,           # 每轮最多试几个升级目标（硬上限；实际还受空闲工人数限制）
    "builders": 5,              # 工人总数（用来算空闲人数）
    "include_walls": False,     # 是否升城墙（默认不升）
    "include_bb": False,        # 是否包含夜世界建筑
    "currency": "any",         # 只升花这种货币的建筑（gold / elixir / dark / any）
    "build_max_h": 168,         # 施工超过这么多小时还没涨级 = 当作状态失效
    "mem_cooldown_h": 24,       # 内存目标成功后多少小时不再碰

    # 分币种的单次花费上限（宝石永远不在列表里，绝不参与）
    "max_cost_gold": 3_000_000,
    "max_cost_elixir": 6_000_000,
    "max_cost_dark": 100_000,
    # 先花哪种资源：圣水满仓会浪费，所以排第一；暗黑量少排第二（只够便宜档）
    "upgrade_currency_priority": ["elixir", "dark", "gold"],

    # 实验室 / 法术工厂里的「研究」（兵种 / 法术）——和「升级建筑本体」是两条路
    "research": True,                       # 是否自动研究
    "research_currency_priority": ["elixir", "dark", "gold"],
    "research_min_gap_min": 30,             # 实验室空着时，最快多少分钟查一次（防反复点手机）
    "research_max_pages": 8,                # 研究对象弹窗最多翻几页
    "research_max_h": 240,                  # 超过这么多小时还没动静 = 当作状态失效
    "research_alternate": True,             # 圣水/暗黑轮换着研究（实验室只有 1 个槽位）
}

KIND_CHOICES = ("gold", "elixir", "dark")
BACKEND_CHOICES = ("ocr", "memory")
CUR_CHOICES = ("gold", "elixir", "dark", "any")
COST_KEYS = (("max_cost_gold", 0, 50_000_000),
             ("max_cost_elixir", 0, 50_000_000),
             ("max_cost_dark", 0, 10_000_000),
             ("research_min_gap_min", 0, 1440),
             ("research_max_pages", 1, 20),
             ("research_max_h", 0, 720))


def _sanitize(d):
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        return out

    sched = d.get("schedule")
    if isinstance(sched, list):
        clean = []
        for s in sched:
            try:
                h, m = str(s).strip().split(":")
                h, m = int(h), int(m)
                if 0 <= h < 24 and 0 <= m < 60:
                    clean.append(f"{h:02d}:{m:02d}")
            except Exception:
                continue
        out["schedule"] = sorted(set(clean))

    for k in ("collect", "upgrade", "include_walls", "include_bb", "research",
              "research_alternate"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]

    bk = d.get("backend")
    if isinstance(bk, str) and bk.strip().lower() in BACKEND_CHOICES:
        out["backend"] = bk.strip().lower()

    kinds = d.get("upgrade_kinds")
    if isinstance(kinds, list):
        out["upgrade_kinds"] = [k for k in kinds if k in KIND_CHOICES]

    for k in ("upgrade_currency_priority", "research_currency_priority"):
        v = d.get(k)
        if isinstance(v, list):
            clean = [c for c in v if c in KIND_CHOICES]
            if clean:
                out[k] = clean

    for k, lo, hi in COST_KEYS + (("max_price", 0, 50_000_000),
                      ("max_upgrades", 0, 5),
                      ("min_free_workers", 0, 5),
                      ("max_targets", 0, 5),
                      ("builders", 0, 10),
                      ("build_max_h", 0, 720),
                      ("mem_cooldown_h", 0, 168)):
        v = d.get(k)
        if isinstance(v, (int, float)) and lo <= v <= hi:
            out[k] = int(v)

    cur = d.get("currency")
    if isinstance(cur, str) and cur.strip().lower() in CUR_CHOICES:
        out["currency"] = cur.strip().lower()
    return out


def load():
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except Exception:
        return dict(DEFAULTS)
    cfg = _sanitize(raw)
    bk = raw.get("backend") if isinstance(raw, dict) else None
    if bk is not None and str(bk).strip().lower() not in BACKEND_CHOICES:
        # 非法值不静默吞掉：打印一条告警并按默认值（ocr）跑，免得看板点了半天没反应
        print(f"[config] backend={bk!r} 非法，只能是 {' / '.join(BACKEND_CHOICES)}；"
              f"已回退到 {cfg['backend']!r}", file=sys.stderr, flush=True)
    return cfg


def save(cfg):
    clean = _sanitize(cfg)
    d = os.path.dirname(CONFIG_PATH) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".coc_cfg_")
    with os.fdopen(fd, "w") as f:
        json.dump(clean, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)
    return clean


def due_now(cfg=None):
    """按北京时间判断当前分钟是否命中排期。"""
    import datetime
    cfg = cfg or load()
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    return now.strftime("%H:%M") in cfg.get("schedule", []), now
