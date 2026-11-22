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
import re
import sys
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import coc_backend_mem as mem
import coc_mem_buildings as bldlib
import coc_mem_panel as panel

STATE = os.path.join(HERE, "coc_auto_state.json")   # 施工跟踪 / 冷却 / 失败计数
LOGD = os.path.join(HERE, "logs")                   # 证据图（面板 / 确认页 / 扣费后）

DIM_TH = 70.0          # 画面平均亮度低于这个 = 有模态框
RECONNECT = [1105, 926]  # 「重新载入游戏」（UI 固定坐标，不随镜头变）
JPG_MATCH_S = 240      # 回填时长时，确认页截图和这笔升级的时间戳最多差这么多秒


def iso_now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def iso_of(ts):
    """epoch → 本地 ISO；拿不到就 None（页面靠 started_ts 算，ISO 只是给人看的）"""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def parse_iso_ts(s):
    """ISO 字符串 → epoch（老记录里 last_round.ts 是 ISO，不是数字）"""
    if not s:
        return None
    try:
        return int(datetime.datetime.fromisoformat(str(s)).timestamp())
    except (TypeError, ValueError):
        return None


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
        # 时间预算（等进村 / 单级定位 / 整轮定位）先从 config 应用到后端模块，
        # 之后所有 relocate / 搜索都按它算，不另传参。
        self.budgets = mem.set_budgets(self.cfg)
        self.s = mem.shell()
        self.ast = jload(STATE, {}) or {}
        self.bld = bldlib.Buildings(s=self.s, log=None if quiet else mem.log)
        self.bstate = {}
        self.n_cand_all = 0
        self.n_plan = 0
        self.cand_head = []
        self.rec = None         # run_round 的进行中结果（超时也能拿来落盘）

    # ---------------- 参数（配置 + 命令行合一） ----------------
    def opt(self, name, default=None):
        v = getattr(self.a, name, None)
        if v is None:
            v = self.cfg.get(name, default)
        return v

    def log(self, msg):
        if not self.quiet:
            mem.log(msg)

    # ---------------- 币种 ----------------
    def caps(self):
        """每个币种的单次花费上限（config 里各管各的；max_price 是老配置的金币上限）"""
        gold = self.opt("max_cost_gold")
        if gold is None:
            gold = self.opt("max_price", 3_000_000)
        return {"gold": int(gold or 0),
                "elixir": int(self.opt("max_cost_elixir", 6_000_000) or 0),
                "dark": int(self.opt("max_cost_dark", 100_000) or 0)}

    def kinds(self):
        """允许花的币种（按优先级排：圣水满仓先花圣水，暗黑量少排第二，金币最后）"""
        ks = self.opt("upgrade_kinds") or ["gold"]
        cur = self.opt("currency")
        if cur and cur != "any":
            ks = [cur]                      # 老配置只写了 currency，就按它来
        prio = self.opt("upgrade_currency_priority") or ["elixir", "dark", "gold"]
        out = [k for k in prio if k in ks]
        return out + [k for k in ("gold", "elixir", "dark") if k in ks and k not in out]

    def cur_prio(self):
        return {c: i for i, c in enumerate(self.kinds())}

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
        self.log(f"  画面偏暗，点「重新载入游戏」@{tuple(mem.RELOAD_BTN)}")
        mem.shell().tap(*mem.RELOAD_BTN, wait=1.2)
        t0 = time.time()
        while time.time() - t0 < 45:
            time.sleep(2.5)
            im = mem.grab()
            m = self.brightness(im)
            if m and m > 90:
                return True, time.time() - t0, im
        return False, time.time() - t0, mem.grab()

    # ---------------- 在建（工人 / 实验室共用的记录视图） ----------------
    def _upgrade_view(self, k, v, now_lv, tnow, max_h):
        """一条在建记录 → 页面/日志要的字段（进度、剩余、时间是否近似）"""
        try:
            nm, lv, _pos = k.split("|")
            lv = int(float(lv))
        except Exception:
            nm, lv = v.get("type") or v.get("name") or k, None
        started = v.get("started_ts") or v.get("ts") or None
        try:
            started = int(float(started)) if started else None
        except (TypeError, ValueError):
            started = None
        dur_s = v.get("duration_s")
        try:
            dur_s = int(float(dur_s)) if dur_s else None
        except (TypeError, ValueError):
            dur_s = None
        age = (tnow - started) if started else None
        item = dict(
            key=k, type=nm, name=v.get("name") or nm,
            from_level=lv, to_level=v.get("target_level") or (lv + 1 if lv is not None else None),
            now_level=now_lv,
            started_ts=started, started_at=v.get("started_at") or iso_of(started),
            duration_s=dur_s, duration_text=v.get("duration_text"),
            duration_source=v.get("duration_source"),
            elapsed_s=int(age) if age is not None else None,
            age_h=round(age / 3600, 1) if age is not None else None,
            cost=v.get("paid") or {}, currency=v.get("currency"),
            price=v.get("price_panel"),
            estimated=bool(v.get("started_estimated") or v.get("duration_estimated")),
            expired=bool(age is not None and age > max_h),
        )
        if dur_s and age is not None:
            item["remaining_s"] = max(0, int(dur_s - age))
            item["progress"] = round(min(1.0, max(0.0, age / dur_s)), 3)
        else:
            item["remaining_s"] = None
            item["progress"] = None       # 时长未知 → 页面不画百分比，只显示已过时间
        return item

    def _duration_from_confirm_jpg(self, type_name, ts):
        """老记录补时长：找这笔升级当时存的确认页截图，把「升级时间」读出来。

        为什么不用「上一轮巡检时间」估：那一轮里其实就存了确认页截图（logs/up_*_confirm_*.jpg），
        上面的时长是真的，读它比估准。找不到截图就返回 None —— 页面显示「时长未知」。
        """
        import glob
        if not type_name or not ts:
            return None
        stem = str(type_name).replace(" ", "_")
        pats = [os.path.join(LOGD, "*_confirm_%s.jpg" % stem),
                os.path.join(LOGD, "*_confirm_%s_*.jpg" % stem)]
        best = None
        for p in [x for pat in pats for x in glob.glob(pat)]:
            m = re.search(r"(\d{8})-(\d{6})", os.path.basename(p))
            if not m:
                continue
            try:
                ft = int(datetime.datetime.strptime(m.group(1) + m.group(2),
                                                    "%Y%m%d%H%M%S").timestamp())
            except ValueError:
                continue
            d = abs(ft - int(ts))
            if d <= JPG_MATCH_S and (best is None or d < best[0]):
                best = (d, p)
        if not best:
            return None
        try:
            im = cv2.imread(best[1])
            d = panel.read_duration(im) if im is not None else {}
        except Exception as e:
            self.log(f"  [builders] 回填时长读图失败 {best[1]}: {e}")
            return None
        if not d.get("seconds"):
            return None
        return dict(seconds=d["seconds"], text=d.get("text"), jpg=os.path.basename(best[1]))

    def backfill_upgrades(self, up=None):
        """给老记录补齐「开始时间 / 总时长」。写回状态，只做一次。

        开始时间：`ts`（真升级那一刻写的）优先；真没有才用最近一轮巡检时间近似，并标 estimated。
        总时长：这条升级真跑过确认页的话，logs 里那张确认页截图上有真值，读出来。
                读不到就留 None，页面显示「时长未知（下一笔起精确）」——不编。
        """
        up = self.ast.get("mem_upgrades") or {} if up is None else up
        if not up:
            return 0
        last_round = parse_iso_ts((self.ast.get("last_round") or {}).get("ts"))
        n = 0
        for k, v in up.items():
            changed = False
            if not v.get("started_ts"):
                ts = v.get("ts") or last_round or int(time.time())
                v["started_ts"] = int(ts)
                v["started_at"] = iso_of(ts)
                v["started_estimated"] = not v.get("ts")   # 用巡检时间凑的才算近似
                changed = True
            if not v.get("target_level"):
                try:
                    v["target_level"] = int(k.split("|")[1]) + 1
                except (IndexError, ValueError):
                    m = re.search(r"\((\d+)\s*[→\-]>?\s*(\d+)\)", str(v.get("name") or ""))
                    if m:
                        v["target_level"] = int(m.group(2))
                changed = True
            if not v.get("currency") and v.get("paid"):
                v["currency"] = next(iter(v["paid"]))
                changed = True
            if not v.get("duration_s"):
                got = self._duration_from_confirm_jpg(v.get("type") or k.split("|")[0],
                                                      v.get("started_ts"))
                if got:
                    v.update(duration_s=got["seconds"], duration_text=got.get("text"),
                             duration_source="confirm_jpg", duration_estimated=False,
                             backfill_jpg=got.get("jpg"))
                    changed = True
                    self.log(f"  [builders] {v.get('name') or k} 补到时长 "
                             f"{got.get('text')}（{got.get('seconds')}s，来自 {got.get('jpg')}）")
            if changed:
                n += 1
        r = self.ast.get("mem_research") or {}
        if r and not r.get("duration_s"):
            # 老研究记录只有 hours（确认页「升级时间」当时只读到了天/小时）
            try:
                hrs = float(r.get("hours") or 0)
            except (TypeError, ValueError):
                hrs = 0
            if hrs:
                r["duration_s"] = int(round(hrs * 3600))
                r["duration_source"] = "hours"
                r["duration_estimated"] = False
                if not r.get("started_ts"):
                    r["started_ts"] = int(float(r.get("ts") or time.time()))
                    r["started_at"] = iso_of(r["started_ts"])
                self.ast["mem_research"] = r
                n += 1
        if n:
            self.ast["mem_upgrades"] = up
            jsave(STATE, self.ast)
        return n

    def builders_state(self, prune=True):
        """数一下还有几个工人在施工 —— 硬目标「工人不闲着」靠这个判断。

        怎么数：mem_upgrades 里每一笔真升级都记了 (类型|等级|x,y)。升级完成时游戏会把
        那栋建筑的等级 +1，所以「状态里记着、而实例等级还是升级前的那个」= 还在施工。
        等级涨了 → 工人已经释放（顺手把这条从状态里清掉，不靠 24h 冷却去猜）。

        另外还有两个上界：
          build_max_h（默认 168h）超过这么久还没涨级 = 当作状态失效，不计入忙碌
          （用户手动取消了施工、或这一笔其实没扣钱时会出现）
        找不到实例（被拆了/坐标变了）→ 也算释放。

        状态迁移分两种，都写进 builds_done（页面上的历史明细读它）：
          done     等级真的涨了 → kind=done，记完成时间 + 实际耗时
          expired  超过 build_max_h 没动静 → kind=expired（「状态失效」，不是「已完成」）
        """
        total = int(self.opt("builders", 5) or 5)
        self.backfill_upgrades()
        up = self.ast.get("mem_upgrades") or {}
        if not up:
            return dict(total=total, busy=0, idle=total, pending=[], in_progress=[], done=0)
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
                done.append((k, v, "expired", None))
                continue
            now_lv = cur.get((nm, x, y)) if rows else None
            age = tnow - float(v.get("started_ts") or v.get("ts") or 0)
            if rows and now_lv is not None and now_lv > lv:
                done.append((k, v, "done", now_lv))      # 等级涨了 = 施工完成，工人已释放
            elif age > max_h:
                done.append((k, v, "expired", now_lv))   # 太久没动静：状态失效
            else:
                busy += 1
                pending.append(self._upgrade_view(k, v, now_lv, tnow, max_h))
        if prune and done:
            hist = self.ast.get("builds_done") or []
            for k, v, kind, now_lv in done:
                up.pop(k, None)
                view = self._upgrade_view(k, v, now_lv, tnow, max_h)
                hist.append(dict(
                    key=k, kind=kind, at=iso_now(), ts=int(tnow),
                    name=view.get("name"), type=view.get("type"),
                    from_level=view.get("from_level"), to_level=view.get("to_level"),
                    now_level=now_lv,
                    started_ts=view.get("started_ts"), started_at=view.get("started_at"),
                    duration_s=view.get("duration_s"), duration_text=view.get("duration_text"),
                    actual_s=view.get("elapsed_s"),
                    cost=view.get("cost"), currency=view.get("currency"),
                    estimated=view.get("estimated")))
            self.ast["mem_upgrades"] = up
            self.ast["builds_done"] = hist[-60:]
        idle = max(0, total - busy)
        return dict(total=total, busy=busy, idle=idle, pending=pending,
                    in_progress=pending, done=len(done),
                    expired=len([d for d in done if d[2] == "expired"]))

    # ---------------- 目标 ----------------
    def mem_targets(self, res=None, T=None):
        """用内存枚举出可升级目标，并算这一轮允许试几个（受空闲工人数约束）。"""
        a = self.a
        try:
            kinds = self.kinds()
            tg = bldlib.discover_upgrade_targets(
                self.bld, res=res, T=T, currency="any",
                max_cost_by_currency={c: self.caps()[c] for c in kinds},
                include_bb=bool(self.opt("include_bb", False)),
                include_walls=bool(self.opt("include_walls", True)),
                tap_dy=self.opt("tap_dy", None),
                allow_unknown_price=True, log=None if self.quiet else mem.log)
        except Exception as e:
            self.log(f"  [targets] 枚举失败: {type(e).__name__}: {e}")
            return []
        up = self.ast.get("mem_upgrades", {})
        bad_tap = set(self.ast.get("mem_bad_tap") or [])
        now = time.time()
        cd = float(self.opt("mem_cooldown_h", 24) or 24) * 3600
        out = []
        prio = self.cur_prio()
        for t in tg:
            if t["currency"] not in prio:
                continue
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
        # 排序：点击点安全的 → 币种优先级（圣水满仓先花圣水）→ 价格已知的 → 便宜的
        out.sort(key=lambda x: (not x["_safe"], prio.get(x["currency"], 9),
                                x["cost"] is None, x["cost"] or 0))
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

        四处硬闸（任何一处不过都不往下走）：
          ① 面板必须开，且标题等级 == 内存等级（否则是点到旁边的墙/别的建筑）
          ② 面板价格必须读出来，币种必须在允许列表里（金币/圣水/暗黑，宝石永远不在）
          ③ 价格 <= 该币种的单次上限（各币种分别设），且该币种库存够
          ④ 必须认到确认页的绿色「确认」按钮才点——认不到就关掉（工人不足/资源不足的弹窗）
        扣费以内存差值为准，和面板价格对不上就记 alert。
        dry=True 只走「读数 + 挑目标」，一次点击都不发。
        """
        rec = None
        tried = 0
        n_ok = 0
        oks = []
        attempts = []
        idle_cap = self.n_plan or 0
        caps = self.caps()
        kinds = self.kinds()
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
            # 这一步是白捡的：即使这一笔因为超上限/币种不够没升，价格也算探到了。
            if (pan.get("open") and pan.get("title_level") == t["_level"]
                    and pan.get("price")):
                try:
                    self.bld.record_price(
                        t["_type"], t["_level"], pan.get("currency") or "unknown",
                        pan["price"],
                        source="panel_prod_red" if pan.get("price_red") else "panel_prod")
                except Exception:
                    pass
            cur = pan.get("currency")
            if not pan.get("open"):
                why = "no_panel"
            elif pan.get("title_level") != t["_level"]:
                why = "wrong_building(title=%s want=%s)" % (pan.get("title_level"),
                                                            t["_level"])
            elif pan.get("price") is None:
                why = "no_price"
            elif cur not in kinds:
                why = "currency_%s" % cur
            elif caps.get(cur) and pan["price"] > caps[cur]:
                why = "over_cap(%d>%d)" % (pan["price"], caps[cur])
            elif pan["price"] > (res.get(cur) or 0):
                why = "insufficient(%d>%d %s)" % (pan["price"], res.get(cur) or 0, cur)
            if why:
                mem.shell().tap(*dis, wait=0.6)
                self._note_fail(t, why)
                self.log(f"  [upgrade] {t['name']} 放弃：{why}")
                rec = dict(target=t["name"], ok=False, why=why, panel=pan)
                attempts.append(dict(target=t["name"], ok=False, why=why))
                continue

            price = int(pan["price"])
            panel_btn = pan["up_btn"]
            price_note = None
            if price_cfg is not None and price_cfg != price:
                price_note = "table=%d panel=%d" % (price_cfg, price)
            # 静态币种表和面板实测不一致时只记一笔（实测为准），不拦
            table_cur = bldlib.currency_of(t["_type"])
            # ③ 点「升级」→ 确认页
            mem.shell().tap(*panel_btn, wait=1.0)
            p2 = self._shot_panel("confirm_%s" % t["_type"])
            im2 = cv2.imread(p2) if p2 else None
            cf = panel.confirm_page(im2) if im2 is not None else {"open": False}
            # 确认页右下角就写着这栋升完要多久（「升级时间 2天1小时53分钟」）：
            # 这一笔的进度条全靠它，读不到就记 None（页面显示「时长未知」，不拿别的数凑）
            drec = panel.read_duration(im2) if (cf.get("open") and im2 is not None) else {}
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
                       currency_panel=cur, cost_cfg=price_cfg, caps=caps,
                       price_note=price_note, panel_jpg=p, confirm_jpg=p2,
                       after_jpg=p3, btn=panel_btn, confirm=cf["btn"],
                       from_level=t["_level"], target_level=t["_level"] + 1,
                       duration_s=drec.get("seconds"), duration_text=drec.get("text"))
            if not paid:
                mem.shell().tap(*dis, wait=0.6)
                self._note_fail(t, "no_charge")
                self.log(f"  [upgrade] {t['name']} 没扣钱（面板 {price}）")
                rec["attempts"] = attempts
                return rec, after
            # 扣费验证：扣的币种必须 == 面板币种，金额 == 面板价（允许 ±25 自然增长）
            got = paid.get(cur, 0)
            rec["match"] = abs(got - price) <= 25
            if not rec["match"]:
                rec["alert"] = "cost_mismatch(panel=%d paid=%d)" % (price, got)
            if set(paid) - {cur}:
                rec["alert"] = "wrong_currency_paid: %s" % paid
            if table_cur != cur:
                rec["table_currency"] = table_cur
            # 扣费核对通过 = 这一笔真的开工了：开始时间 + 面板读到的总时长一起落盘，
            # 页面上的进度条和倒计时就靠这两个数（游戏关着也能算，纯时间推算）
            started = int(time.time())
            self.ast.setdefault("mem_upgrades", {})[t["_key"]] = dict(
                ts=started, started_ts=started, started_at=iso_of(started),
                started_estimated=False,
                cd=float(self.opt("mem_cooldown_h", 24) or 24) * 3600,
                name=t["name"], type=t["_type"], key=t["_key"],
                from_level=t["_level"], target_level=t["_level"] + 1,
                paid=paid, cost_paid=sum(paid.values()) if paid else None,
                price_panel=price, currency=cur,
                duration_s=drec.get("seconds"), duration_text=drec.get("text"),
                duration_source=("confirm_panel" if drec.get("seconds") else None),
                duration_estimated=False)
            # 实测扣费就是真价 → 回填价格表（源=upgrade_delta）
            # 但「面板价和表里的价不一致」时不写表：那说明点开的可能是同等级的另一栋
            # （实测点加农炮会点到同格的隐形炸弹），写进去就把表污染了
            trusted = self.bld.price_trusted(t["_type"], t["_level"])
            if price_cfg is None or price_cfg == price or not trusted:
                self.bld.record_price(t["_type"], t["_level"], cur or "gold", got or price,
                                      source="upgrade_delta")
            else:
                rec["price_skipped"] = "table=%d panel=%d（不覆盖价格表）" % (price_cfg, price)
                self.log(f"  [upgrade] 面板价 {price} 与表里 {price_cfg} 不一致，不写价格表")
            mem.shell().tap(*dis, wait=0.3)
            self.log(f"  [upgrade] ✓ {t['name']} 扣 {paid}（面板 {price} {cur}，"
                     f"匹配 {rec['match']}）")
            rec["attempts"] = attempts
            oks.append(rec)
            n_ok += 1
            res = after                       # 下面「够不够」用扣完之后的数
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

    # ---------------- 研究 ----------------
    def run_research(self, res, dry=False):
        """实验室研究一步。节流：忙时只看状态不开面板；闲时节流 N 分钟查一次。

        为什么节流：cron 每 10 分钟一轮，而研究要占实验室好几个小时。忙的时候每轮都去
        开实验室面板 = 白点几十下（手机发烫），所以先用状态里的「在研究什么 + 多久」判断。
        """
        import coc_mem_research as research
        busy = research.Research(s=self.s, bld=self.bld, cfg=self.cfg,
                                 log=self.log).busy_reason()
        if busy and not dry:
            self.log(f"  [research] 实验室在忙：{busy}")
            return dict(busy=busy, skipped="researching")
        last = (self.ast.get("mem_research_check") or {}).get("ts", 0) or 0
        gap = float(self.opt("research_min_gap_min", 30) or 0) * 60
        if not dry and last and time.time() - last < gap:
            return dict(skipped="gap", next_in=int(gap - (time.time() - last)))
        self.ast["mem_research_check"] = dict(ts=int(time.time()), at=iso_now())
        try:
            return research.run(self, res, dry=dry)
        except Exception as e:
            self.log(f"  [research] 失败 {type(e).__name__}: {e}")
            return dict(alerts=["research_error:%s" % type(e).__name__])

    def dismiss_modals(self, im=None, tries=2):
        """关掉挡在村庄前面的推广弹层（礼包 / 商店 / 活动）。

        为什么要单独一步：这类弹层会把整屏盖住，采集点和实验室点击全落在弹层上，
        一轮下来一个资源收不到、面板也开不了，而且它自己会定时冒出来（实测 20:12 冒了一个「专属礼包」）。
        判据是「右上角那个红 X」——干净村庄/建筑面板上没有这个红斑。
        """
        import coc_mem_research as rsch
        for _ in range(max(1, tries)):
            im = im if im is not None else mem.grab()
            if im is None:
                return None
            pos = rsch.popup_x(im)
            if not pos:
                # 再顺手确认没有建筑面板挡着（比如点到树上弹出的「移树」面板）：
                # 有面板时采集点会点在面板上，一轮下来一个资源都收不到
                pan = panel.read(im)
                if pan.get("open") and pan.get("cards"):
                    self.log("  先取消掉挡着的建筑面板（点空地）")
                    mem.shell().tap(300, 700, wait=0.8)
                    im = None
                    continue
                return im
            self.log(f"  关掉挡屏弹层 @{pos}")
            mem.shell().tap(*pos, wait=1.2)
            im = None
        return mem.grab()

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
                   calib=None, alerts=[], duration=None, dur_s=None, error=None)
        alerts = rec["alerts"]
        self.rec = rec          # 边跑边挂在实例上：被看门狗切断时 main 还能落一份部分结果
        if run_dir:
            os.makedirs(run_dir, exist_ok=True)

        # 1) 一轮开始：冷启动（唤醒 + 解锁 → 没跑就拉起 → 轮询等进村 → 断线框点重新载入）
        cs = None
        try:
            cs = mem.start_round(cold=bool(self.opt("cold_start", True)),
                                 wait=self.opt("village_wait_s"))
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = f"冷启动异常: {type(e).__name__}: {e}"
            alerts.append("cold_start_error")
            rec["duration"] = rec["dur_s"] = round(time.time() - t0, 1)
            return rec
        rec["cold_start"] = cs
        if not cs.get("ok"):
            err = cs.get("error") or "unknown"
            rec["status"] = "failed"
            rec["error"] = f"冷启动失败: {err}"
            # 掉登录页是红线：不尝试自己登，只写 alert 退出
            alerts.append("login_page" if "login" in err else "cold_start_failed")
            log(f"  ⚠ {rec['error']}")
            rec["duration"] = rec["dur_s"] = round(time.time() - t0, 1)
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
                rec["duration"] = rec["dur_s"] = round(time.time() - t0, 1)
                return rec

        # 2.5) 先把挡屏的推广弹层关掉，不然下面的采集点和实验室点击全落在弹层上
        if not dry:
            _im = self.dismiss_modals(img)
            if _im is not None:
                img = _im

        # 3) 读资源（地址失效会自己重定位）
        mem.LOCATE_INFO = {}            # 清一下，好知道这一轮里到底重定位过没有
        res = self.read()
        relocated = False
        if mem.LOCATE_INFO:
            # read_all 内部自己重定位了（冷启动后 pid 变了就是这条），也要记进 meta
            rec["relocate"] = dict(mem.LOCATE_INFO)
            relocated = True
        if res is None and not relocated:
            # 只有「read 里没试过定位」时才再试一次。已经试过并且失败的，
            # 原地重试一遍只是把等待翻倍（种子、画面、pid 一模一样）。
            ok_loc = mem.relocate()
            rec["relocate"] = dict(ok=ok_loc, **(mem.LOCATE_INFO or {}))
            relocated = True
            if not ok_loc and mem.LOCATE_INFO.get("timed_out"):
                alerts.append("locate_timeout")
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
            rec["duration"] = rec["dur_s"] = round(time.time() - t0, 1)
            return rec
        rec["before"] = res
        res0 = dict(res)
        if run_dir:
            self.shot(os.path.join(run_dir, "before.jpg"))

        # 4) 镜头自校正（锚点 + 气泡残差）
        T = mem.calibrate(img)
        if (T or {}).get("via") in ("cached", "identity"):
            # 缓存标定在镜头被游戏自己推走之后是错的（采集点会整体偏 100px 以上）。
            # 用实验室贴图反解一次平移量，修好之后再往下走。
            try:
                import coc_mem_research as _rsch
                fixed = _rsch.fix_calib_from_template(self.bld, T, im=img)
            except Exception as e:
                fixed = None
                self.log(f"  [calib] 贴图修标定失败: {type(e).__name__}: {e}")
            if fixed:
                T = fixed
                rec["calib_fixed"] = fixed
                self.log("  [calib] 用实验室贴图修正镜头：s=%.4f tx=%.1f ty=%.1f（分数 %.2f）"
                         % (fixed["s"], fixed["tx"], fixed["ty"],
                            fixed.get("anchor_score") or 0))
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
                expired=bs.get("expired"),
                pending=[p.get("name") for p in (bs.get("pending") or [])],
                # 在建明细（开始时间 / 总时长 / 剩余 / 进度）—— 看板「正在升级」读它，
                # 同时也是每轮的历史快照，事后能对着 history/<id>/meta.json 复盘
                in_progress=bs.get("in_progress") or [])
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
            # 带上 ok：coc_patrol_run 靠这一位判断「真升了」，丢了它 meta 里
            # upgrade 永远是 null（花了钱、看板还显示没升级，实测踩过）
            rec["upgrade"] = dict(ok=True, name=up.get("target"),
                                  price=up.get("price_panel"),
                                  paid=up.get("paid"), match=up.get("match"),
                                  n_ok=up.get("n_ok"))
        n_ok = int((up or {}).get("n_ok") or (1 if (up or {}).get("ok") else 0))
        rec["n_ok"] = n_ok
        if rec.get("builders"):
            rec["builders"]["idle_after"] = max(
                0, int(rec["builders"].get("idle") or 0) - n_ok)

        # 6.5) 实验室研究（只有 1 个槽位，和工人的逻辑不同：不看工人数，看实验室忙不忙）
        rs = None
        if upgrade and self.opt("research", True):
            rs = self.run_research(after_collect, dry=dry)
            rec["research"] = rs
            rres = (rs or {}).get("result") or {}
            if rres.get("ok"):
                after_collect = rres.get("after") or after_collect

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

        # 硬报警：资源总量不该变少（除了升级 / 研究扣费）
        paid = dict((up or {}).get("paid", {}) if up else {})
        rs_paid = ((rs or {}).get("result") or {}).get("paid") or {}
        for c, v in rs_paid.items():
            paid[c] = paid.get(c, 0) + v
        for k in ("gold", "elixir", "dark"):
            g = rec["gain"].get(k)
            if g is not None and g + (paid.get(k) or 0) < -1:
                alerts.append("resource_dropped_%s_%d" % (k, g))
        rec["spent"] = paid
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
        rec["dur_s"] = rec["duration"]
        rs_txt = ""
        if rs:
            if (rs.get("result") or {}).get("ok"):
                r0 = rs["result"]
                rs_txt = " | 研究 %s→%s级 %s" % (r0.get("name"), r0.get("target_level"),
                                                 r0.get("paid"))
            elif rs.get("busy"):
                rs_txt = " | 实验室在忙: %s" % rs["busy"]
            elif rs.get("picked"):
                rs_txt = " | 研究候选 %s %s %s" % (
                    (rs.get("picked") or {}).get("name") or "?",
                    (rs.get("picked") or {}).get("price"),
                    (rs.get("picked") or {}).get("currency"))
        self.log(f"一轮结束 {rec['duration']}s | 资源 {res0} → {final} | "
                 f"工人 {rec['builders']} | 升级 {n_ok} 笔 | 花费 {paid}{rs_txt}")
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
    ap.add_argument("--backfill", action="store_true",
                    help="只给在建记录补「开始时间 / 总时长」，不开游戏、不点任何东西")
    a = ap.parse_args()
    cfg = cfg_of(max_targets=a.max_targets, max_cost=a.max_cost, currency=a.currency,
                 builders=a.builders, include_bb=a.include_bb,
                 include_walls=a.include_walls)
    auto = MemAuto(cfg, a, quiet=a.quiet)
    try:
        if a.backfill:
            n = auto.backfill_upgrades()
            print(json.dumps(dict(filled=n,
                                  mem_upgrades=auto.ast.get("mem_upgrades") or {}),
                             ensure_ascii=False, indent=1, default=float))
            return 0
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
