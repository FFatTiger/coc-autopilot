#!/usr/bin/env python3
"""部落冲突定时巡检：进村 -> 收资源 -> 读工人/星星 -> 建筑列表巡检 -> 有工人且付得起就升一级。

安全边界：
- 只点收集气泡和建筑列表里的行，不盲点坐标；
- 只升级 gold/elixir 项目，暗黑重油项目一律不碰（留给人工）；
- 单次升级价格上限 MAX_PRICE；
- 任何面板认不出就用 BACK + 点空地退出，不做猜测性点击。
"""
import json
import os
import shutil
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from coc_patrol2 import (sh, grab, enter_village, read_res_vote, read_workers, read_star,
                         find_bubbles, collect_one, init_capture, close_capture, panel_open,
                         clear_panels, open_build_list, read_build_list, upgrade_from_list,
                         read_res_until, log, PKG, ACT)
import coc_config

HIST = os.environ.get("COC_HISTORY") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
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


def main():
    cfg = coc_config.load()
    if "--if-due" in sys.argv:
        ok, now = coc_config.due_now(cfg)
        if not ok:
            return 0                       # 没到点，静默退出（cron 每小时叫一次）
        log(f"到点执行 {now:%Y-%m-%d %H:%M} (北京时间)")

    run_id = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(HIST, run_id)
    os.makedirs(run_dir, exist_ok=True)
    meta = {"id": run_id, "start": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "running", "before": None, "after": None, "gain": None,
            "workers": None, "star": None, "bubbles": 0, "collected": 0,
            "items": [], "upgrade": None, "upgrades": [], "duration": None,
            "error": None, "config": cfg}
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
        meta.update({"after": after, "gain": gain, "workers": list(w),
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
                                           w0=w if _n == 0 else None)
                    log(f"升级结果 {up[0] if up else None}")
                    if not up:
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
        meta["duration"] = round(time.time() - t0, 1)
        meta["end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            with open(os.path.join(run_dir, "meta.json"), "w") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log(f"写 meta 失败 {e}")
        log(f"用时 {meta['duration']}s，已熄屏，记录 {run_id}")
        prune()


if __name__ == "__main__":
    sys.exit(main())
