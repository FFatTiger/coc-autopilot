#!/usr/bin/env python3
"""「正在升级」的数据层：把 coc_auto_state.json 里的在建记录算成页面要的那一份。

为什么单独一个模块
  游戏一天只开 4 次（巡检跑完就关），中间内存读不到，所以进度只能「已落盘的开始时间
  + 面板上读到的总时长」做时间推算。真正确认完成要等下一轮巡检（发现等级涨了）。
  这个模块不碰手机、不碰内存，纯读状态文件 + 算数，看板和以后别的脚本都能直接用。

字段契约（采集侧 coc_mem_auto / coc_mem_research 写，这里读）：
  mem_upgrades[key] = {
    ts, started_ts, started_at,        # 开工时刻（epoch / 本地 ISO）
    name, type, from_level, target_level,
    duration_s, duration_text,         # 确认页「升级时间 2天1小时53分钟」读到的秒数
    duration_source, started_estimated, duration_estimated,
    paid {币种: 数量}, price_panel, currency,
  }
  mem_research = 同构（hours 是老字段，没有 duration_s 时拿它 ×3600 兜底）
  builds_done / research_done = 完成历史（kind: done / expired）

用法：
  python3 coc_progress.py            # 打印当前在建（人看的表格）
  python3 coc_progress.py --json     # 打印页面接口那份 JSON
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("COC_AUTO_STATE") or os.path.join(HERE, "coc_auto_state.json")

CUR_ZERO = 0


def _load(path=None):
    try:
        with open(path or STATE) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _iso(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _num(v):
    try:
        f = float(v)
        return f if f == f else None        # nan 丢掉
    except (TypeError, ValueError):
        return None


def _parse_name(name):
    """「Wizard Tower(9→10)」→ (type, 9, 10)；解析不出就返回 (name, None, None)"""
    m = re.match(r"^(.*?)\s*[（(]\s*(\d+)\s*(?:→|->|→|—>|~)\s*(\d+)\s*[)）]\s*$",
                 str(name or ""))
    if m:
        return m.group(1).strip(), int(m.group(2)), int(m.group(3))
    return (str(name or ""), None, None)


def _cost(paid):
    """{gold: 1200000} → (1200000, 'gold')；多币种就加起来（实际上不会发生）"""
    if not isinstance(paid, dict) or not paid:
        return None, None
    cur = None
    tot = 0
    for k, v in paid.items():
        n = _num(v)
        if n:
            tot += int(n)
            cur = cur or k
    return (tot or None), cur


def _item(kind, rec, now, default_total=None):
    """一条在建 → 页面卡片要的字段（进度 / 剩余 / 是否近似 / 是否超期）"""
    if not isinstance(rec, dict):
        return None
    name = rec.get("name") or ""
    typ = rec.get("type") or _parse_name(name)[0]
    _t, nl_from, nl_to = _parse_name(name)
    from_level = rec.get("from_level")
    to_level = rec.get("target_level")
    from_level = int(from_level) if _num(from_level) is not None else nl_from
    to_level = int(to_level) if _num(to_level) is not None else nl_to
    started = _num(rec.get("started_ts")) or _num(rec.get("ts"))
    dur = _num(rec.get("duration_s"))
    if not dur:
        hrs = _num(rec.get("hours"))
        dur = hrs * 3600 if hrs else None
    elapsed = (now - started) if started else None
    out = dict(
        kind=kind, key=rec.get("key") or name, type=typ,
        name=(rec.get("name") if kind == "research" else typ),
        label=name,
        from_level=from_level, to_level=to_level,
        started_ts=int(started) if started else None,
        started_at=rec.get("started_at") or _iso(started),
        duration_s=int(dur) if dur else None,
        duration_text=rec.get("duration_text"),
        duration_source=rec.get("duration_source"),
        elapsed_s=int(elapsed) if elapsed is not None else None,
        estimated=bool(rec.get("started_estimated") or rec.get("duration_estimated")),
        expired=bool(elapsed is not None and default_total
                     and elapsed > default_total),
    )
    cost, cur = _cost(rec.get("paid"))
    out["cost"], out["currency"] = cost, (cur or rec.get("currency"))
    if dur and elapsed is not None:
        out["remaining_s"] = max(0, int(dur - elapsed))
        out["progress"] = round(min(1.0, max(0.0, elapsed / dur)), 4)
        out["overdue"] = elapsed >= dur
    else:
        out["remaining_s"] = None
        out["progress"] = None
        out["overdue"] = False
    return out


def build(state=None, now=None, total_builders=5, build_max_h=168, research_max_h=240):
    """状态 → {now, items, building, research, busy, idle, total, done}"""
    st = _load() if state is None else (state or {})
    now = int(now or time.time())
    max_b = float(build_max_h or 168) * 3600
    max_r = float(research_max_h or 240) * 3600

    items, done = [], []
    for k, v in (st.get("mem_upgrades") or {}).items():
        it = _item("building", dict(v, key=k), now, max_b)
        if not it:
            continue
        if it["expired"]:
            # 超过兜底时长：状态失效（多半是手动取消了施工 / 这笔其实没扣钱），不当在建
            continue
        items.append(it)

    r = st.get("mem_research") or {}
    if r:
        it = _item("research", r, now, max_r)
        if it and not it["expired"]:
            items.append(it)

    # 排序：剩余时间短的在前（快好了的先看到），未知时长的垫底
    items.sort(key=lambda i: (i["remaining_s"] is None, i["remaining_s"] or 0))
    busy = len([i for i in items if i["kind"] == "building"])
    for h in (st.get("builds_done") or [])[-8:]:
        done.append(dict(kind="building", at=h.get("at"), ts=h.get("ts"),
                         name=h.get("name"), type=h.get("type"),
                         from_level=h.get("from_level"), to_level=h.get("to_level"),
                         now_level=h.get("now_level"), state=h.get("kind"),
                         actual_s=h.get("actual_s"), duration_s=h.get("duration_s"),
                         started_ts=h.get("started_ts"), cost=h.get("cost"),
                         currency=h.get("currency")))
    for h in (st.get("research_done") or [])[-8:]:
        done.append(dict(kind="research", at=h.get("at"), ts=h.get("ts"),
                         name=h.get("name"), type="研究",
                         from_level=h.get("from_level"), to_level=h.get("to_level"),
                         state=h.get("kind"), actual_s=h.get("actual_s"),
                         duration_s=h.get("duration_s"), started_ts=h.get("started_ts"),
                         cost=h.get("cost"), currency=h.get("currency")))
    done.sort(key=lambda d: d.get("ts") or 0, reverse=True)

    return dict(now=now, now_iso=_iso(now),
                items=items,
                building=busy, research=1 if any(i["kind"] == "research" for i in items) else 0,
                busy=busy, idle=max(0, int(total_builders) - busy),
                total=int(total_builders),
                done=done[:6],
                last_round=(st.get("last_round") or {}).get("ts"))


def fmt_s(sec):
    if sec is None:
        return "—"
    sec = int(max(0, sec))
    if sec <= 0:
        return "0 分"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%d天%d小时" % (d, h)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分" % m


def main(argv=None):
    ap = argparse.ArgumentParser(description="在建进度（时间推算）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state", default=None)
    a = ap.parse_args(argv)
    st = _load(a.state) if a.state else None
    try:
        import coc_config
        cfg = coc_config.load()
    except Exception:
        cfg = {}
    d = build(st, total_builders=cfg.get("builders", 5),
              build_max_h=cfg.get("build_max_h", 168),
              research_max_h=cfg.get("research_max_h", 240))
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    print("工人 %d/%d 忙 · 实验室 %s" % (d["busy"], d["total"],
                                    "忙" if d["research"] else "空闲"))
    for i in d["items"]:
        bar = ""
        if i["progress"] is not None:
            n = int(round(i["progress"] * 24))
            bar = "[" + "#" * n + "-" * (24 - n) + "] %3d%%" % round(i["progress"] * 100)
        tag = " 实验室" if i["kind"] == "research" else ""
        print("%-8s %-26s %-34s 剩 %-12s 已过 %-10s%s%s" % (
            "研究" if i["kind"] == "research" else "建筑",
            i["label"][:26], bar, fmt_s(i["remaining_s"]), fmt_s(i["elapsed_s"]),
            " 时间近似" if i["estimated"] else "",
            " 时长未知" if i["duration_s"] is None else tag))
    if not d["items"]:
        print("（没有在建：工人全空闲）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
