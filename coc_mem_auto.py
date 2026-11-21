#!/usr/bin/env python3
"""内存后端的一轮巡检：唤醒 → 进村 → 收资源 → 数工人 → 内存枚举升级目标 → 面板三级校验升级。

这是 config 里 backend = "memory" 时 coc_patrol_run.py 真正跑的东西。它和 OCR 后端的
区别不只是「数字从哪来」，更是「升级怎么保证不点错」：

  决策全部来自内存：资源值、增量、工人占用、建筑类型/等级/坐标、扣费验证。
  截图只做两件联锁的事：① 画面变暗 = 弹了模态框；② 锚点/气泡反解镜头缩放平移，
  把标定坐标映射到当前画面。三层面板校验（打开→标题等级→价格币种→确认页）保证
  每一次真实点击都对准了「这一栋的升级按钮」，任一试不过就关面板退出。

硬目标「别让工人闲着」：
  每轮先数空闲工人（按建筑池里的实际等级涨没涨判断，不靠冷却时间猜），
  每轮最多试 min(候选数, 空闲工人数, max_targets) 个目标，一个工人都没空就一个面板都不开。

用法（一般不用直接跑，走 coc_patrol_run.py --backend memory）:
  python3 coc_mem_auto.py --dry            # 只读不点，打印一轮计划
  python3 coc_mem_auto.py --plan           # 只打印内存枚举出的升级计划
  python3 coc_mem_auto.py                  # 真跑一轮（收资源 + 按需升级）
"""
import argparse
import datetime
import json
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import coc_backend_mem as mem
import coc_mem_buildings as bldlib
import coc_mem_panel as panel

STATE = os.path.join(HERE, "coc_auto_state.json")   # 施工跟踪 / 冷却 / 失败计数
LOGD = os.path.join(HERE, "logs")                   # 证据图（面板 / 确认页 / 扣费后）

DIM_TH = 70.0          # 画面平均亮度低于这个 = 有模态框
RECONNECT = [1105, 926]  # 「重新载入游戏」（UI 固定坐标，不随镜头变）


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
    # default=float：镜头解算出来的值可能是 numpy 标量，json 不认
    fd = open(tmp, "w")
    try:
        json.dump(obj, fd, ensure_ascii=False, indent=1, default=float)
    finally:
        fd.close()
    os.replace(tmp, p)


class MemAuto:
    """一轮巡检的执行者。一个进程用一个实例，run_round() 可重复调用。"""

    def __init__(self, cfg, args=None, quiet=False):
        self.cfg = cfg or {}
        self.a = args or argparse.Namespace()
        self.quiet = quiet
        self.s = mem.shell()
        self.ast = jload(STATE, {}) or {}
        self.bld = bldlib.Buildings(s=self.s, log=None if quiet else mem.log)
        self.bstate = {}
        self.n_cand_all = 0
        self.n_plan = 0
        self.cand_head = []

    # ---------------- 参数（配置 + 命令行合一） ----------------
    def opt(self, name, default=None):
        v = getattr(self.a, name, None)
        if v is None:
            v = self.cfg.get(name, default)
        return v

    def log(self, msg):
        if not self.quiet:
            mem.log(msg)

    # ---------------- 读数 ----------------
    def read(self, retries=2):
        """读一次四项资源；读不到返回 None（read_all 内部会尝试重定位）。"""
        return mem.read_all(retries=retries)

    def shot(self, path, quality=82):
        """存一张 JPEG 证据图（原始 PNG 4.6MB，JPEG 质量 82 约 250KB）。"""
        tmp = path.rsplit(".", 1)[0] + ".png"
        if not mem.screencap(tmp):
            return None
        try:
            im = cv2.imread(tmp)
            if im is not None:
                cv2.imwrite(path, im, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                return path
        except Exception:
            return tmp
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None

    def brightness(self, img):
        if img is None:
            return None
        return float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())

    # ---------------- 弹窗 ----------------
    def handle_modal(self, tries=18):
        """画面偏暗 = 模态框。断线框点「重新载入游戏」，之后等亮度恢复。"""
        self.log(f"  画面偏暗，点「重新载入游戏」@{tuple(RECONNECT)}")
        mem.shell().tap(*RECONNECT, wait=1.2)
        t0 = time.time()
        while time.time() - t0 < 45:
            time.sleep(2.5)
            im = mem.grab()
            m = self.brightness(im)
            if m and m > 90:
                return True, time.time() - t0, im
        return False, time.time() - t0, mem.grab()

    # ---------------- 工人 ----------------
    def builders_state(self, prune=True):
        """数一下还有几个工人在施工 —— 硬目标「工人不闲着」靠这个判断。

        怎么数：mem_upgrades 里每一笔真升级都记了 (类型|等级|x,y)。升级完成时游戏会把
        那栋建筑的等级 +1，所以「状态里记着、而实例等级还是升级前的那个」= 还在施工。
        等级涨了 → 工人已经释放（顺手把这条从状态里清掉，不靠 24h 冷却去猜）。

        另外还有两个上界：
          build_max_h（默认 168h）超过这么久还没涨级 = 当作状态失效，不计入忙碌
          （用户手动取消了施工、或这一笔其实没扣钱时会出现）
        找不到实例（被拆了/坐标变了）→ 也算释放。
        """
        total = int(self.opt("builders", 5) or 5)
        up = self.ast.get("mem_upgrades") or {}
        if not up:
            return dict(total=total, busy=0, idle=total, pending=[], done=0)
        try:
            rows = self.bld.snapshot()
        except Exception as e:
            self.log(f"  [builders] 读建筑池失败: {e}")
            rows = []
        cur = {(r["name"], r["x"], r["y"]): r["level"] for r in rows}
        max_h = float(self.opt("build_max_h", 168) or 168) * 3600
        tnow = time.time()
        busy, pending, done = 0, [], []
        for k, v in list(up.items()):
            try:
                nm, lv, pos = k.split("|")
                lv = int(float(lv))
                x, y = [float(s) for s in pos.split(",")]
            except Exception:
                done.append(k)
                continue
            now_lv = cur.get((nm, x, y)) if rows else None
            age = tnow - float(v.get("ts", 0) or 0)
            if rows and now_lv is not None and now_lv > lv:
                done.append(k)                      # 等级涨了 = 施工完成，工人已释放
            elif age > max_h:
                done.append(k)                      # 太久没动静：状态失效
            else:
                busy += 1
                pending.append(dict(key=k, name=v.get("name"), level=lv,
                                    now_level=now_lv, age_h=round(age / 3600, 1)))
        if prune and done:
            for k in done:
                up.pop(k, None)
            self.ast["mem_upgrades"] = up
            self.ast["builds_done"] = (self.ast.get("builds_done") or [])[-40:] + [
                dict(key=k, at=iso_now()) for k in done]
        idle = max(0, total - busy)
        return dict(total=total, busy=busy, idle=idle, pending=pending, done=len(done))

    # ---------------- 目标 ----------------
    def mem_targets(self, res=None, T=None):
        """用内存枚举出可升级目标，并算这一轮允许试几个（受空闲工人数约束）。"""
        a = self.a
        try:
            tg = bldlib.discover_upgrade_targets(
                self.bld, res=res, T=T, currency=self.opt("currency", "gold"),
                max_cost=self.opt("max_cost", 3_000_000),
                include_bb=bool(self.opt("include_bb", False)),
                include_walls=bool(self.opt("include_walls", True)),
                tap_dy=self.opt("tap_dy", None),
                allow_unknown_price=True, log=None if self.quiet else mem.log)
        except Exception as e:
            self.log(f"  [targets] 枚举失败: {e}")
            return []
        up = self.ast.get("mem_upgrades", {})
        bad_tap = set(self.ast.get("mem_bad_tap") or [])
        now = time.time()
        cd = float(self.opt("mem_cooldown_h", 24) or 24) * 3600
        out = []
        for t in tg:
            key = "%s|%d|%.1f,%.1f" % (t["name"], t["level"], t["x"], t["y"])
            last = up.get(key)
            if last and now - last.get("ts", 0) < last.get("cd", cd):
                continue
            if key in bad_tap:
                continue                     # 这个实例的点位就是点不中，别再浪费点击
            sx, sy = t["screen"]
            if not (8 <= sx <= 2212 and 8 <= sy <= 1072):
                continue                      # 屏幕外的点不到，别浪费面板探测
            safe = bool(230 <= sx <= 1900 and 150 <= sy <= 900)
            out.append(dict(name="%s(%d→%d)" % (t["name"], t["level"], t["level"] + 1),
                            tap=t["calib"], currency=t["currency"],
                            cost=t["price"], _key=key, _safe=safe,
                            _level=t["level"], _type=t["name"], _dy=t.get("tap_dy"),
                            _world=[t["x"], t["y"]], _screen=t["screen"]))
        # 便宜的、价格已知的、点击点安全的排前面
        out.sort(key=lambda x: (not x["_safe"], x["cost"] is None, x["cost"] or 0))
        self.n_cand_all = len(out)          # 截断前的真实候选数（日志里要如实反映）
        bs = self.builders_state()
        self.bstate = bs
        # 每轮最多试「候选数」和「空闲工人数」里小的那个；
        # 一个工人都没空就一个面板都不用开（不开面板 = 不浪费点击 = 不发热）
        cap = min(len(out), bs["idle"])
        max_targets = int(self.opt("max_targets", 3) or 0)
        if max_targets:
            cap = min(cap, max_targets)
        self.n_plan = max(0, cap)
        self.cand_head = out[:5]            # 只有观测价值：工人全忙时也把候选记进日志
        if bs["idle"] <= 0:
            self.bstate["why_skip"] = "no_idle_builder"
            return []
        self.bstate["why_skip"] = None
        return out[:cap]

    # ---------------- 升级 ----------------
    def _shot_panel(self, name):
        os.makedirs(LOGD, exist_ok=True)
        base = os.path.join(LOGD, "up_%s_%s" % (
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
            name.replace(" ", "_")))
        return self.shot(base + ".jpg")

    def _note_fail(self, t, why):
        fails = self.ast.setdefault("mem_fails", {})
        k = t["_key"]
        fails[k] = fails.get(k, 0) + 1
        self.ast.setdefault("upgrade_fails", []).append(
            dict(target=t["name"], ok=False, why=why, at=iso_now()))
        self.ast["upgrade_fails"] = self.ast["upgrade_fails"][-20:]
        if fails[k] >= 2 and why in ("no_price", "no_charge"):
            bldlib.note_blocked(t["_type"], t["_level"], reason=why)
        if fails[k] >= 2 and (why.startswith("wrong_building") or why == "no_panel"):
            # 点位问题：只封这一个实例，别把整个类型封掉
            lst = self.ast.setdefault("mem_bad_tap", [])
            if k not in lst:
                lst.append(k)
                self.log(f"  [upgrade] {t['name']} 点位连续 2 次点不中，实例加入 mem_bad_tap")

    def upgrade_targets(self, tg, dis, res, dry=False):
        """点建筑 → 面板 → 点「升级」→ 确认页 → 点「确认」，每步都验，任一步不符就退出。

        三处硬闸（任何一处不过都不往下走）：
          ① 面板必须开，且标题等级 == 内存等级（否则是点到旁边的墙/别的建筑）
          ② 面板价格必须读出来、币种必须是 gold、价格 <= 上限、且金币够
          ③ 必须认到确认页的绿色「确认」按钮才点——认不到就关掉（工人不足/资源不足的弹窗）
        扣费以内存差值为准，和面板价格对不上就记 alert。
        dry=True 只走「读数 + 挑目标」，一次点击都不发。
        """
        rec = None
        tried = 0
        n_ok = 0
        oks = []
        attempts = []
        idle_cap = self.n_plan or 0
        max_cost = self.opt("max_cost")
        max_targets = int(self.opt("max_targets", 3) or 0)
        for t in tg:
            if max_targets and tried >= max_targets:
                break
            if idle_cap and n_ok >= idle_cap:
                break
            tried += 1
            if dry:
                rec = dict(target=t["name"], ok=False, why="dry", price=t.get("cost"),
                           currency=t.get("currency"), tap=[round(v, 1) for v in t["_screen"]],
                           safe=bool(t.get("_safe", True)))
                attempts.append(dict(target=t["name"], ok=False, why="dry"))
                self.log(f"  [upgrade] dry：候选 {t['name']} {t.get('cost')} "
                         f"{t.get('currency')} @{t['_screen']}，不点击")
                continue
            x, y = t["_screen"]
            price_cfg = t.get("cost")
            mem.shell().tap(*dis, wait=0.45)               # 先关掉可能开着的面板
            mem.shell().tap(x, y, wait=0.95)
            p = self._shot_panel("panel_%s" % t["_type"])
            im = cv2.imread(p) if p else None
            mean = self.brightness(im)
            if mean is not None and mean < DIM_TH:
                self.handle_modal()
                self._note_fail(t, "modal")
                return dict(target=t["name"], ok=False, why="modal",
                            attempts=attempts), res
            pan = panel.read(im) if im is not None else {"open": False}
            why = None
            # 面板一开、标题等级对上，就把读到的价格写进价格表（source=panel_prod）。
            # 这一步是白捡的：即使这一笔因为超上限/金币不够没升，价格也算探到了。
            if (pan.get("open") and pan.get("title_level") == t["_level"]
                    and pan.get("price")):
                # 红字（买不起）读出来的价只当参考值：红字抗锯齿后笔画会糊，
                # 实测把 2,800,000 读成过 2,000,000（8 被认成 0）
                try:
                    self.bld.record_price(
                        t["_type"], t["_level"], pan.get("currency") or "unknown",
                        pan["price"],
                        source="panel_prod_red" if pan.get("price_red") else "panel_prod")
                except Exception:
                    pass
            if not pan.get("open"):
                why = "no_panel"
            elif pan.get("title_level") != t["_level"]:
                why = "wrong_building(title=%s want=%s)" % (pan.get("title_level"),
                                                            t["_level"])
            elif pan.get("price") is None:
                why = "no_price"
            elif pan.get("currency") != "gold":
                why = "currency_%s" % pan.get("currency")
            elif bldlib.currency_of(t["_type"]) != "gold":
                # 双保险（红线：只花金币）：面板认成金币还不够，这个类型本身按静态
                # 表也必须花金币。金矿/储金罐/兵营/实验室这些实际花圣水的，一律不进。
                why = "type_currency_%s" % bldlib.currency_of(t["_type"])
            elif max_cost and pan["price"] > max_cost:
                why = "over_cap(%d>%d)" % (pan["price"], max_cost)
            elif pan["price"] > (res.get("gold") or 0):
                why = "insufficient(%d>%d)" % (pan["price"], res.get("gold") or 0)
            if why:
                mem.shell().tap(*dis, wait=0.6)
                self._note_fail(t, why)
                self.log(f"  [upgrade] {t['name']} 放弃：{why}")
                rec = dict(target=t["name"], ok=False, why=why, panel=pan)
                attempts.append(dict(target=t["name"], ok=False, why=why))
                # 这几类只是「这个目标不合适」，换下一个目标继续试；
                # no_price 连续 2 次会进 building_blocked.json，以后不再选它
                continue

            price = int(pan["price"])
            panel_btn = pan["up_btn"]
            price_note = None
            if price_cfg is not None and price_cfg != price:
                price_note = "table=%d panel=%d" % (price_cfg, price)
            # ③ 点「升级」→ 确认页
            mem.shell().tap(*panel_btn, wait=1.0)
            p2 = self._shot_panel("confirm_%s" % t["_type"])
            im2 = cv2.imread(p2) if p2 else None
            cf = panel.confirm_page(im2) if im2 is not None else {"open": False}
            if not cf.get("open") or not cf.get("btn"):
                mem.shell().tap(*cf.get("close", (1926, 76)), wait=0.8)
                mem.shell().tap(*dis, wait=0.6)
                self._note_fail(t, "no_confirm_page")
                self.log(f"  [upgrade] {t['name']} 放弃：没认到确认页（可能工人不足/资源不足）")
                attempts.append(dict(target=t["name"], ok=False, why="no_confirm_page"))
                return dict(target=t["name"], ok=False, why="no_confirm_page",
                            attempts=attempts, panel_jpg=p, confirm_jpg=p2), res
            before = self.read(retries=1) or res
            mem.shell().tap(*cf["btn"], wait=1.9)          # 点「确认」= 真正扣费
            after = self.read(retries=2) or before
            spent = {c: before[c] - after[c] for c in ("gold", "elixir", "dark")}
            paid = {c: v for c, v in spent.items() if v > 0}
            p3 = self._shot_panel("after_%s" % t["_type"])
            rec = dict(target=t["name"], ok=bool(paid), price_panel=price,
                       paid=paid, before=dict(before), after=dict(after),
                       currency_panel=pan.get("currency"), cost_cfg=price_cfg,
                       price_note=price_note, panel_jpg=p, confirm_jpg=p2,
                       after_jpg=p3, btn=panel_btn, confirm=cf["btn"])
            if not paid:
                mem.shell().tap(*dis, wait=0.6)
                self._note_fail(t, "no_charge")
                self.log(f"  [upgrade] {t['name']} 没扣钱（面板 {price}）")
                rec["attempts"] = attempts
                return rec, after
            # 扣费验证：金币差应该 == 面板价（允许 ±25 自然增长）
            got = paid.get("gold", 0)
            rec["match"] = abs(got - price) <= 25
            if not rec["match"]:
                rec["alert"] = "cost_mismatch(panel=%d paid=%d)" % (price, got)
            if any(paid.get(c) for c in ("elixir", "dark")):
                rec["alert"] = "wrong_currency_paid: %s" % paid
            self.ast.setdefault("mem_upgrades", {})[t["_key"]] = dict(
                ts=int(time.time()),
                cd=float(self.opt("mem_cooldown_h", 24) or 24) * 3600,
                name=t["name"], paid=paid, price_panel=price)
            # 实测扣费就是真价 → 回填价格表（源=upgrade_delta）
            # 但「面板价和表里的价不一致」时不写表：那说明点开的可能是同等级的另一栋
            # （实测点加农炮会点到同格的隐形炸弹），写进去就把表污染了
            trusted = self.bld.price_trusted(t["_type"], t["_level"])
            if price_cfg is None or price_cfg == price or not trusted:
                cur = "gold" if got else next(iter(paid), "gold")
                self.bld.record_price(t["_type"], t["_level"], cur, got or price,
                                      source="upgrade_delta")
            else:
                rec["price_skipped"] = "table=%d panel=%d（不覆盖价格表）" % (price_cfg, price)
                self.log(f"  [upgrade] 面板价 {price} 与表里 {price_cfg} 不一致，不写价格表")
            mem.shell().tap(*dis, wait=0.3)
            self.log(f"  [upgrade] ✓ {t['name']} 扣 {paid}（面板 {price}，"
                     f"匹配 {rec['match']}）")
            rec["attempts"] = attempts
            oks.append(rec)
            n_ok += 1
            res = after                       # 下面的「金币够不够」用扣完之后的数
            continue                          # 还有空工人就继续下一个目标
        if oks:
            total_paid = {}
            for r in oks:
                for c, v in (r.get("paid") or {}).items():
                    total_paid[c] = total_paid.get(c, 0) + v
            last = oks[-1]
            out = dict(last)
            out.update(ok=True, n_ok=n_ok, paid=total_paid, upgrades=oks,
                       match=all(r.get("match") for r in oks),
                       before=oks[0].get("before"), after=last.get("after"),
                       attempts=attempts)
            alerts = [r["alert"] for r in oks if r.get("alert")]
            if alerts:
                out["alert"] = "; ".join(alerts)
            return out, res
        if rec is not None:
            rec["attempts"] = attempts
            return rec, res
        return None, res

    # ---------------- 一轮 ----------------
    def run_round(self, run_dir=None, dry=False, collect=True, upgrade=True):
        """跑一轮，返回写进 meta.json 的那份 dict（截图落到 run_dir）。"""
        t0 = time.time()
        rec = dict(id=os.path.basename(run_dir) if run_dir else None,
                   start=iso_now(), status="running", backend="memory", dry=dry,
                   sources=dict(res="mem", workers="ocr", star="ocr",
                                builders="mem", upgrade="mem-panel"),
                   before=None, after=None, gain=None, final=None,
                   bubbles=0, collected=0, items=[],
                   upgrade=None, upgrades=[], builders=None,
                   n_candidates=0, n_plan=0, candidates=[],
                   calib=None, alerts=[], duration=None, error=None)
        alerts = rec["alerts"]
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)

        # 1) 屏幕亮 + 游戏在前台 + pid 有效
        try:
            changed, why = mem.ensure_ready()
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = f"游戏拉不起来: {e}"
            alerts.append("app_not_ready")
            rec["duration"] = round(time.time() - t0, 1)
            return rec
        rec["ensure"] = why
        if "relaunch" in why:
            # 刚拉起来的游戏要几十秒才把村庄和资源结构建好；这时去读内存，
            # 老地址读不到东西，重定位还会在「结构还没出现」的内存里瞎搜。
            time.sleep(10)
            if mem.enter_village() is None:
                rec["status"] = "failed"
                rec["error"] = "重开游戏后进村失败"
                alerts.append("enter_village_failed")
                rec["duration"] = round(time.time() - t0, 1)
                return rec

        # 2) 抓一帧：画面偏暗先当模态框处理
        img = mem.grab()
        mean = self.brightness(img)
        rec["brightness"] = round(mean, 1) if mean else None
        if mean is not None and mean < DIM_TH:
            okm, secs, img = self.handle_modal()
            rec["modal"] = dict(detected=True, recovered=okm, sec=round(secs, 1))
            alerts.append("modal_detected")
            rec["brightness"] = round(self.brightness(img) or 0, 1) if img is not None else None
            if okm and mem.enter_village() is None:
                rec["status"] = "failed"
                rec["error"] = "断线重载后进村失败"
                alerts.append("enter_village_failed")
                rec["duration"] = round(time.time() - t0, 1)
                return rec

        # 3) 读资源（地址失效会自己重定位）
        res = self.read()
        relocated = False
        if res is None:
            rec["relocate"] = mem.relocate()
            relocated = True
            res = self.read()
            if res is not None:
                bad = mem.check_against_ocr(res)
                if bad:
                    # 定到的地址和画面对不上：不认这个地址，把画面读数存成种子，
                    # 下一轮从画面种子重新定位（否则会一直照着假结构往下算）
                    rec["hud_mismatch"] = bad
                    alerts.append("relocate_hud_mismatch")
                    mem.invalidate("round_hud_mismatch")
                    res = None
        if res is None:
            rec["status"] = "failed"
            rec["error"] = "memory_unreadable（地址定位失败）"
            alerts.append("memory_unreadable")
            rec["duration"] = round(time.time() - t0, 1)
            return rec
        rec["before"] = res
        res0 = dict(res)
        if run_dir:
            self.shot(os.path.join(run_dir, "before.jpg"))

        # 4) 镜头自校正（锚点 + 气泡残差）
        T = mem.calibrate(img)
        rec["calib"] = T
        if not T or T.get("via") in (None, "identity"):
            alerts.append("calib_identity")
        pts, dis = mem.collect_points(T)

        # 5) 收资源（dry 只列点位不点）
        if collect:
            b1, a1, ok, items = mem.collect(img=img, dry=dry, T=T)
            rec["items"] = items
            rec["collected"] = 0 if dry else ok
            rec["bubbles"] = len([p for p in pts if len(p) == 3])
            after_collect = self.read() or a1 or res
        else:
            rec["bubbles"] = len([p for p in pts if len(p) == 3])
            after_collect = res
        rec["after_collect"] = after_collect

        # 6) 升级目标 + 工人状态（dry 也照常算，好让看板看到「为什么没升」）
        tg = []
        up = None
        if upgrade:
            tg = self.mem_targets(after_collect, T)
            bs = self.bstate or {}
            rec["builders"] = dict(
                total=bs.get("total"), busy=bs.get("busy"), idle=bs.get("idle"),
                skip=bs.get("why_skip"), freed=bs.get("done"),
                pending=[p.get("name") for p in (bs.get("pending") or [])])
            rec["n_candidates"] = self.n_cand_all
            rec["n_plan"] = self.n_plan
            shown = tg[:5] if tg else (self.cand_head or [])
            rec["candidates"] = [dict(name=t["name"], cost=t.get("cost"),
                                      safe=bool(t.get("_safe", True)),
                                      tap=[round(v, 1) for v in t["_screen"]])
                                 for t in shown]
            if not dry:
                up, after_collect = self.upgrade_targets(tg, dis, after_collect, dry=False)
            elif tg:
                up, _ = self.upgrade_targets(tg, dis, after_collect, dry=True)
        rec["upgrade"] = up
        if up and up.get("ok"):
            rec["upgrades"] = up.get("upgrades") or [up]
            rec["upgrade"] = dict(name=up.get("target"), price=up.get("price_panel"),
                                  paid=up.get("paid"), match=up.get("match"))
        n_ok = int((up or {}).get("n_ok") or (1 if (up or {}).get("ok") else 0))
        rec["n_ok"] = n_ok
        if rec.get("builders"):
            rec["builders"]["idle_after"] = max(
                0, int(rec["builders"].get("idle") or 0) - n_ok)

        # 7) 收尾读数 + 增量
        if not dry:
            time.sleep(1.0)
            mem.clear_panels()
            time.sleep(2.0)                 # 升级/收集后资源栏有高亮动画，读早了会拿到残值
        final = self.read() or after_collect
        if final.get("gold") is None and after_collect.get("gold") is not None:
            final = after_collect
            self.log("  最终读数为空，用上一读兜底")
        rec["after"] = after_collect
        rec["final"] = final
        rec["gain"] = {k: (final[k] - res0[k]) if (final.get(k) is not None
                                                   and res0.get(k) is not None) else None
                       for k in ("gold", "elixir", "dark")}
        rec["delta"] = rec["gain"]

        # 硬报警：资源总量不该变少（除了升级扣费）
        paid = (up or {}).get("paid", {}) if up else {}
        for k in ("gold", "elixir", "dark"):
            g = rec["gain"].get(k)
            if g is not None and g + (paid.get(k) or 0) < -1:
                alerts.append("resource_dropped_%s_%d" % (k, g))
        notes = []
        if (final.get("elixir") or 0) >= 11_000_000:
            notes.append("圣水 11,000,000 已满，圣水收集增量恒为 0")
        if (final.get("gold") or 0) >= 9_000_000:
            notes.append("金币接近/达到上限")
        rec["notes"] = notes

        # 8) 存种子 + 施工跟踪状态
        mem.remember_sample(final)
        self.ast["last_round"] = dict(ts=rec["start"], before=res0, after=after_collect,
                                      delta=rec["gain"], upgrade=up, alerts=alerts)
        jsave(STATE, self.ast)

        if run_dir:
            img2 = mem.grab()
            if img2 is not None:
                cv2.imwrite(os.path.join(run_dir, "after.jpg"), img2,
                            [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        rec["status"] = "ok"
        rec["duration"] = round(time.time() - t0, 1)
        self.log(f"一轮结束 {rec['duration']}s | 资源 {res0} → {final} | "
                 f"工人 {rec['builders']} | 升级 {n_ok} 笔")
        return rec

    def close(self):
        try:
            self.bld.close()
        except Exception:
            pass
        mem.close()


# ---------------------------------------------------------------- CLI
def cfg_of(**kw):
    """命令行优先、其次 coc_config，最后落在这里的默认值。"""
    import coc_config
    cfg = coc_config.load()
    for k, v in kw.items():
        if v is not None:
            cfg[k] = v
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只读不点")
    ap.add_argument("--plan", action="store_true", help="只打印升级计划，一次点击都不发")
    ap.add_argument("--no-collect", action="store_true")
    ap.add_argument("--no-upgrade", action="store_true")
    ap.add_argument("--max-targets", type=int, default=None)
    ap.add_argument("--max-cost", type=int, default=None)
    ap.add_argument("--currency", default=None)
    ap.add_argument("--builders", type=int, default=None)
    ap.add_argument("--include-bb", dest="include_bb", action="store_true", default=None)
    ap.add_argument("--no-walls", dest="include_walls", action="store_false", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    cfg = cfg_of(max_targets=a.max_targets, max_cost=a.max_cost, currency=a.currency,
                 builders=a.builders, include_bb=a.include_bb,
                 include_walls=a.include_walls)
    auto = MemAuto(cfg, a, quiet=a.quiet)
    try:
        if a.plan:
            res = auto.read()
            img = mem.grab()
            T = mem.calibrate(img)
            tg = auto.mem_targets(res, T)
            rows = auto.bld.snapshot()
            print(json.dumps(dict(res=res, calib=T, pool=auto.bld.locate(),
                                  n_buildings=len(rows),
                                  builders=auto.bstate, n_candidates=auto.n_cand_all,
                                  n_plan=auto.n_plan, plan=tg),
                             ensure_ascii=False, indent=1, default=float))
            return 0
        rec = auto.run_round(dry=a.dry, collect=not a.no_collect,
                             upgrade=not a.no_upgrade)
        print(json.dumps(rec, ensure_ascii=False, default=float))
        return 0 if rec.get("status") == "ok" else 1
    finally:
        auto.close()


if __name__ == "__main__":
    sys.exit(main())
