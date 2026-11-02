#!/usr/bin/env python3
"""巡检配置读写：单一 JSON 文件，看板和巡检脚本共用。

时区处理：cron 每小时触发一次，脚本自己按北京时间判断"到点没"，
这样改配置不需要动 crontab，也不受美国夏令时切换影响。
"""
import json
import os
import tempfile

CONFIG_PATH = os.environ.get("COC_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {
    "schedule": ["09:00", "13:00", "18:00", "22:00"],   # 北京时间，HH:MM
    "collect": True,            # 是否收资源
    "upgrade": True,            # 是否自动升级
    "upgrade_kinds": ["gold", "elixir"],   # 允许消耗的资源类型，dark 需显式开启
    "max_price": 3_000_000,     # 单次升级价格上限
    "max_upgrades": 1,          # 每轮最多升几个
    "min_free_workers": 1,      # 至少留几个空闲工人（1 = 有工人就升）
}

KIND_CHOICES = ("gold", "elixir", "dark")


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

    for k in ("collect", "upgrade"):
        if isinstance(d.get(k), bool):
            out[k] = d[k]

    kinds = d.get("upgrade_kinds")
    if isinstance(kinds, list):
        out["upgrade_kinds"] = [k for k in kinds if k in KIND_CHOICES]

    for k, lo, hi in (("max_price", 0, 50_000_000),
                      ("max_upgrades", 0, 5),
                      ("min_free_workers", 0, 5)):
        v = d.get(k)
        if isinstance(v, (int, float)) and lo <= v <= hi:
            out[k] = int(v)
    return out


def load():
    try:
        with open(CONFIG_PATH) as f:
            return _sanitize(json.load(f))
    except Exception:
        return dict(DEFAULTS)


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
