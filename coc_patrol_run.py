#!/usr/bin/env python3
"""部落冲突巡检：唯一的生产入口。读数后端由 config 的 backend 决定（--backend 可临时覆盖）。

两种后端对外接口一致，差别只在「数字从哪来、升级怎么验」：

- backend = "ocr"    截图 + OCR + 模板匹配，模块 coc_patrol2。每轮 force-stop 重开游戏进村。
- backend = "memory" 读 /proc/<pid>/mem，模块 coc_backend_mem（需要 root）。
                     一轮由 coc_mem_auto 执行：收资源、数工人、内存枚举建筑、
                     三层面板校验升级。不重开游戏（靠常驻进程保活、地址才长期有效）。

安全边界（两种后端一样）：
- 只点收集气泡（内存后端是标定表里的采集点）和「升级」按钮，不盲点坐标；
- 花的币种由 upgrade_kinds 决定（默认 gold / elixir / dark 三种都开）；宝石永远不花；
- 单次花费上限按币种分别设（max_cost_gold / max_cost_elixir / max_cost_dark），
  面板上的币种必须在这个白名单里才点确认；每轮最多 max_upgrades 笔、最多试 max_targets 个目标；
- 实验室研究（兵种 / 法术）走 coc_mem_research：只占实验室那 1 个槽位、不占工人；
  大本营 / 城堡 / 英雄建筑 / 实验室与法术工厂的**建筑本体升级**仍然禁止；
- 任何面板认不出就用 BACK + 点空地退出，不做猜测性点击。

每轮记录落在 history/<时间戳>/：meta.json + before.jpg + after.jpg，看板读的就是它。

用法:
  python3 coc_patrol_run.py                        # 真跑一轮（后端按 config）
  python3 coc_patrol_run.py --backend memory       # 强制内存后端跑一轮
  python3 coc_patrol_run.py --dry                  # 只读不点：读数、采集点、升级候选、可研究对象
  python3 coc_patrol_run.py --if-due               # 给 cron 用，没到点就静默退出（仅 OCR 后端）
  python3 coc_patrol_run.py --backend memory --dry # 内存后端空跑一轮（迁代码/验证用）
"""
import argparse
import json
import os
import shutil
import sys
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coc_config


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="部落冲突巡检（生产入口）")
    ap.add_argument("--backend", choices=("ocr", "memory"), default=None,
                    help="临时覆盖 config 里的 backend")
    ap.add_argument("--dry", action="store_true", help="只读不点")
    ap.add_argument("--if-due", dest="if_due", action="store_true",
                    help="没到 schedule 里的点就静默退出（OCR 后端用）")
    ap.add_argument("--no-collect", dest="no_collect", action="store_true")
    ap.add_argument("--no-upgrade", dest="no_upgrade", action="store_true")
    # 下面这几个只对内存后端有意义，不填就按 config
    ap.add_argument("--max-targets", type=int, default=None, help="每轮最多试几个升级目标")
    ap.add_argument("--max-cost", type=int, default=None, help="单次升级价格上限")
    ap.add_argument("--currency", default=None, help="只升这个货币的建筑（默认 gold）")
    ap.add_argument("--builders", type=int, default=None, help="工人总数（默认 5）")
    ap.add_argument("--include-bb", dest="include_bb", action="store_true", default=None,
                    help="包含夜世界建筑")
    ap.add_argument("--no-walls", dest="include_walls", action="store_false", default=None,
                    help="不升城墙")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


ARGS = parse_args()
CFG0 = coc_config.load()
BACKEND = ARGS.backend or CFG0.get("backend", "ocr")

# 后端只在这一处分叉：两边导出同名函数，下面的流程一个字都不用改。
if BACKEND == "memory":
    from coc_backend_mem import (sh, grab, enter_village, read_res_vote, read_workers,
                                 read_star, find_bubbles, collect_one, init_capture,
                                 close_capture, panel_open, clear_panels, open_build_list,
                                 read_build_list, upgrade_from_list, read_res_until, log,
                                 PKG, ACT)
else:
    from coc_patrol2 import (sh, grab, enter_village, read_res_vote, read_workers, read_star,
                             find_bubbles, collect_one, init_capture, close_capture, panel_open,
                             clear_panels, open_build_list, read_build_list, upgrade_from_list,
                             read_res_until, log, PKG, ACT)

# 每项读数的来源，写进 meta.json 给看板看
if BACKEND == "memory":
    SOURCES = {"res": "mem", "workers": "ocr", "star": "ocr",
               "builders": "mem", "upgrade": "mem-panel"}
else:
    SOURCES = {"res": "ocr", "workers": "ocr", "star": "ocr"}

HIST = os.environ.get("COC_HISTORY") or os.path.join(HERE, "history")
KEEP_DAYS = 30          # 历史保留天数，截图每轮约 1.4MB，30 天 × 4 轮 ≈ 170MB


def shot(img, path):
    """存盘快照，JPEG 质量 82 控制在 200KB 以内。"""
    if img is None:
        return False
    try:
        cv2.imwrite(path, img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return True
    except Exception:
        return False


def prune(days=KEEP_DAYS):
    """删掉超过保留期的历史目录。"""
    if not os.path.isdir(HIST):
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for name in os.listdir(HIST):
        d = os.path.join(HIST, name)
        if not os.path.isdir(d):
            continue
        try:
            if os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except Exception:
            pass
    if n:
        log(f"清理旧记录 {n} 条（保留 {days} 天）")
    return n


def write_meta(run_dir, meta):
    try:
        with open(os.path.join(run_dir, "meta.json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1, default=float)
    except Exception as e:
        log(f"写 meta 失败 {e}")


# ---------------------------------------------------------------- 内存后端
def run_memory(cfg, run_dir, meta):
    """内存后端一轮：交给 coc_mem_auto 跑，回来补齐看板要的通用字段。"""
    from coc_mem_auto import MemAuto

    auto = MemAuto(cfg, ARGS, quiet=ARGS.quiet)
    try:
        rec = auto.run_round(run_dir=run_dir, dry=ARGS.dry,
                             collect=cfg["collect"] and not ARGS.no_collect,
                             upgrade=cfg["upgrade"] and not ARGS.no_upgrade)
    finally:
        auto.close()
    meta.update({k: rec.get(k) for k in
                 ("before", "after", "final", "gain", "delta", "collected", "bubbles",
                  "builders", "n_candidates", "n_plan", "research", "spent",
                  "candidates", "calib", "brightness", "ensure", "notes", "n_ok")
                 if rec.get(k) is not None})
    # 看板的「可升级项」读的是 items（OCR 后端填的是建筑列表行），内存后端这里换成
    # 内存枚举出来的候选：名字里带「当前级→下一级」，价格没有就留 None（看板显示 —）。
    meta["items"] = [dict(name=c.get("name"), price=c.get("cost"),
                         kind=c.get("currency") or "gold")
                     for c in (rec.get("candidates") or [])]
    meta["collect_items"] = rec.get("items") or []
    # 升级结果分三种：真升了 / dry 只挑不点 / 试了没成。看板只认「真升了」，
    # 所以 dry 的候选单独放 dry_upgrade，别让看板把候选显示成已升级。
    up = rec.get("upgrade") or {}
    if up.get("ok"):
        meta["upgrade"] = up
        meta["upgrades"] = rec.get("upgrades") or [up]
    elif up.get("why") == "dry":
        meta["dry_upgrade"] = dict(name=up.get("target"), price=up.get("price"))
        meta["dry_candidates"] = [a.get("target") for a in (up.get("attempts") or [])]
    elif up:
        meta["upgrade_fail"] = dict(name=up.get("target"), why=up.get("why"))
    meta["alerts"] = rec.get("alerts") or []
    meta["status"] = rec.get("status") or "error"
    if rec.get("error"):
        meta["error"] = rec["error"]
    if rec.get("modal"):
        meta["modal"] = rec["modal"]
    if rec.get("relocate"):
        meta["relocate"] = rec["relocate"]

    # 工人/星星的内存读数还没定位（是 UI 文本），补一次 OCR 给看板卡片用。
    # 内存的 builders 更准（按等级是否上涨判断在施工），两个都给看板。
    try:
        img = grab()
        w = read_workers(img)
        meta["workers"] = list(w)
        meta["star"] = read_star(img)
    except Exception as e:
        meta["alerts"].append(f"ocr_workers_failed: {type(e).__name__}")
    return 0 if meta["status"] == "ok" else 1


# ---------------------------------------------------------------- OCR 后端
def run_ocr(cfg, run_dir, meta):
    """OCR 后端一轮：原巡检流程（截图识别 + 列表升级），一并写同一份 meta。"""
    init_capture()
    t0 = time.time()
    try:
        sh("input keyevent KEYCODE_WAKEUP")
        time.sleep(2)
        sh(f"am force-stop {PKG}")
        time.sleep(1)
        sh(f"am start -n {PKG}/{ACT}")
        img = enter_village()
        if img is None:
            log("进村失败，放弃本轮")
            meta["status"] = "failed"
            meta["error"] = "进村失败"
            return 1
        time.sleep(4.0)                     # 进村后数字还在滚动，读早了会拿到上一屏的残值
        shot(img, os.path.join(run_dir, "before.jpg"))

        before = read_res_vote(3)
        for _ in range(3):                  # 数字没读全就再等一轮
            if before.get("gold") and before.get("elixir") and before.get("dark") is not None:
                break
            time.sleep(1.5)
            before = read_res_vote(3)
        log(f"收取前 {before}")
        meta["before"] = before

        bubbles = find_bubbles(img) if cfg["collect"] else []
        for _ in range(3):
            if bubbles or not cfg["collect"]:
                break
            time.sleep(2.0)
            bubbles = find_bubbles(grab())
        log(f"气泡 {len(bubbles)} 个" + ("" if cfg["collect"] else "（已关闭收集）"))

        ok = 0
        for i, (kind, x, y, _sc) in enumerate(bubbles):
            if ARGS.dry:                     # --dry：不点击，只把目标列出来
                log(f"  [{i+1}/{len(bubbles)}] {kind} ({x},{y}) dry，不点击")
                continue
            if time.time() - t0 > 240:
                log(f"时间到，停在 {i}/{len(bubbles)}")
                break
            st, _shot = collect_one(x, y)
            if st in ("direct", "collect"):
                ok += 1
            log(f"  [{i+1}/{len(bubbles)}] {kind} ({x},{y}) {st}")

        clear_panels()
        time.sleep(3.0)                     # 收集后有金色高亮动画，OCR 会认成乱码，等它结束
        after = read_res_until(3, tries=4, gap=1.2)
        w = read_workers(grab())
        star = read_star(grab())
        gain = {k: (after[k] - before[k]) if after.get(k) is not None and before.get(k) is not None else None
                for k in ("gold", "elixir", "dark")}
        log(f"收取后 {after} | 成功 {ok}/{len(bubbles)} | 收益 {gain}")
        log(f"建筑工人 {w} | 胜利之星 {star}")
        meta.update({"after": after, "gain": gain, "delta": gain, "workers": list(w),
                     "star": star, "bubbles": len(bubbles), "collected": ok})

        bl = open_build_list()
        if bl is not None:
            items = read_build_list(bl)
            log(f"可升级 {len(items)} 项: " +
                "; ".join(f"{i['name']}({i['price']:,},{i['kind']})" for i in items))
            meta["items"] = items
            free_w = w[0] if w[0] is not None else 0   # (空闲, 总数)，不要再减
            rounds = cfg["max_upgrades"] if cfg["upgrade"] else 0
            if free_w >= cfg["min_free_workers"] and items and rounds > 0:
                for _n in range(rounds):
                    # 列表已经开着，把读到的 items 直接传进去，避免重复点锤子把它关掉
                    up = upgrade_from_list(max_price=cfg["max_price"],
                                           kinds=tuple(cfg["upgrade_kinds"]),
                                           items=items if _n == 0 else None,
                                           res=after if _n == 0 else None,   # 用收集后已读到的值，避免再读一次拿到空
                                           w0=w if _n == 0 else None,
                                           dry=ARGS.dry)
                    log(f"升级结果 {up[0] if up else None}")
                    if not up:
                        break
                    if ARGS.dry:                      # 只挑不点：记下候选，别当成真升过
                        meta["dry_upgrade"] = {"name": up[0]["name"], "price": up[0]["price"],
                                               "kind": up[0]["kind"]}
                        break
                    meta["upgrades"].append({"name": up[0]["name"], "price": up[0]["price"],
                                             "kind": up[0]["kind"]})
                    meta["upgrade"] = meta["upgrades"][-1]
                    free_w -= 1
                    if free_w < cfg["min_free_workers"]:
                        break
            else:
                log(f"跳过升级 空闲工人={free_w} 允许升级={rounds}（需 ≥{cfg['min_free_workers']}）")
            time.sleep(1.0)
            clear_panels()
            time.sleep(2.5)                 # 同上：升级后资源栏也有高亮
            final_img = grab()
            shot(final_img, os.path.join(run_dir, "after.jpg"))
            final = read_res_vote(3)
            if final.get("gold") is None and after.get("gold") is not None:
                final = after                 # 面板没关干净时读数会全空，退回收取后的值
                log("最终读数为空，用收取后的值兜底")
            log(f"最终 {final} | 工人 {read_workers(final_img)} | 星星 {read_star(final_img)}")
            meta["final"] = final
        meta["status"] = "ok"
        return 0
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = f"{type(e).__name__}: {e}"
        log(f"异常 {meta['error']}")
        return 1
    finally:
        close_capture()
        sh(f"am force-stop {PKG}")
        sh("input keyevent KEYCODE_SLEEP")


# ---------------------------------------------------------------- 主流程
def main():
    cfg = coc_config.load()
    if ARGS.backend:
        cfg["backend"] = ARGS.backend
    # 命令行覆盖配置（只对内存后端有意义，OCR 后端读不到这些键也不受影响）
    for cli, key in (("max_targets", "max_targets"), ("max_cost", "max_price"),
                     ("currency", "currency"), ("builders", "builders"),
                     ("include_bb", "include_bb"), ("include_walls", "include_walls")):
        v = getattr(ARGS, cli, None)
        if v is not None:
            cfg[key] = v
    dry = ARGS.dry

    if ARGS.if_due and BACKEND != "memory":
        ok, now = coc_config.due_now(cfg)
        if not ok:
            return 0                       # 没到点，静默退出（cron 每小时叫一次）
        log(f"到点执行 {now:%Y-%m-%d %H:%M} (北京时间)")

    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(HIST, run_id)
    os.makedirs(run_dir, exist_ok=True)
    meta = {"id": run_id, "start": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "running", "backend": BACKEND, "dry": dry, "sources": SOURCES,
            "before": None, "after": None, "gain": None, "final": None,
            "workers": None, "star": None, "bubbles": 0, "collected": 0,
            "items": [], "upgrade": None, "upgrades": [], "dry_upgrade": None,
            "builders": None, "alerts": [], "duration": None, "error": None,
            "config": cfg}
    t0 = time.time()
    rc = 1
    try:
        if BACKEND == "memory":
            rc = run_memory(cfg, run_dir, meta)
        else:
            rc = run_ocr(cfg, run_dir, meta)
    finally:
        meta["duration"] = round(time.time() - t0, 1)
        meta["end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        write_meta(run_dir, meta)
        log(f"用时 {meta['duration']}s，记录 {run_id}（{BACKEND} 后端）")
        prune()
    return rc


if __name__ == "__main__":
    sys.exit(main())
