#!/usr/bin/env python3
"""内存后端（config 里 backend = "memory"）：资源读数、收集判定、扣费验证全部读
`/proc/<pid>/mem`，只有「点哪里」还依赖画面（模板匹配标定镜头 + 面板守卫）。

为什么值得做：OCR 把「6,065,002」拆成三段拼、数字滚动时会认错一位，收取后的增量
只能按帧投票估算；内存里 gold/elixir/dark 是四个连续 uint32，一次 `dd | od` 约
70ms 读完，逐位精确。升级扣费验证可以做到「一分不差」，地址失效还能自己找回来。

对外接口与 coc_patrol2 保持一致，`coc_patrol_run.py` 只换 import 就能切换：

    read_res(img=None) / read_res_vote(n) / read_res_until(n, tries, gap)
    read_res_stable(prev, tag, tries)
    collect(img=None, dry=False) -> (before, after, ok, items)
    find_bubbles(img) -> [(kind, x, y, score)]       # 标定表算出的采集点
    read_workers(img) / read_star(img)               # 内存里还没定位，降级走 OCR
    upgrade_from_list(max_price, kinds, dry, items, res, w0)
    以及抓帧/进村/面板守卫这些 UI 原语（全部转调 coc_patrol2）

内存模式的额外依赖：设备 root（Magisk su）、adb、以及运行期状态文件
`coc_state.json`（资源地址，丢了会自己重新定位）。

手动用法:
  python3 coc_backend_mem.py read                # 读一次资源（地址没了会自动 locate）
  python3 coc_backend_mem.py locate              # 强制重定位（带时间上限）
  python3 coc_backend_mem.py cold                # 一轮开头：唤醒解锁 → 拉起 → 等进村
  python3 coc_backend_mem.py village             # 只判一下当前是什么画面
  python3 coc_backend_mem.py close               # 杀游戏并回读 pidof 确认
  python3 coc_backend_mem.py sleep               # 锁屏并回读 dumpsys 确认
  python3 coc_backend_mem.py power               # 打印 mWakefulness / Display Power
  python3 coc_backend_mem.py shutdown            # 收尾：关游戏 + 锁屏
  python3 coc_backend_mem.py calib               # 解一次镜头变换并打印
  python3 coc_backend_mem.py collect --dry       # 走一遍 17 个采集点，不点击
  python3 coc_backend_mem.py collect             # 真收一轮（点气泡 + dismiss）
"""
import base64
import json
import os
import re
import select
import subprocess
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import coc_patrol2 as _ocr
from coc_patrol2 import (sh, enter_village, collect_one, init_capture, close_capture,
                         panel_open, clear_panels, open_build_list, read_build_list,
                         read_workers as _ocr_read_workers, read_star as _ocr_read_star,
                         tap as _ocr_tap, log, PKG, ACT)

BACKEND = "memory"
SERIAL = _ocr.SERIAL


def _detect_serial():
    """问 adb 要唯一在线设备的 serial（可能有多个时返回空，交给 adb 自己报错）。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return ""
    devs = []
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 2 and f[1] == "device":
            devs.append(f[0])
    return devs[0] if len(devs) == 1 else ""


if not SERIAL:
    # coc_patrol2 的 sh() / screencap 把 serial 写死进命令行（["adb", "-s", SERIAL, ...]）。
    # ANDROID_SERIAL 没设时会退化成 `adb -s ''` —— adb 回 "error: device '' not found"，
    # rc=1 但不抛异常。后果是面板守卫、点空地关面板、进村流程里的点击全部静默失效
    # （读数正常、点了没反应，这是最难查的一类）。这里把真 serial 回填给它。
    SERIAL = _detect_serial()
    _ocr.SERIAL = SERIAL

# 全屏弹层（「欢迎来到公共频道」「城镇广场」这类活动推广页）：会盖住村庄中间、
# 把背景压暗。内存读数不受影响（照样读），但工人/星星 OCR、建筑列表、采集点
# 全被挡住。它不在 coc_patrol2.classify() 认识的弹窗里，内存后端自己收一道。
PROMO_WORDS = ("前往查看", "公共频道", "城镇广场", "欢迎来到")
PROMO_CLOSE = (1938, 120)      # 弹层右上角的 X，和 coc_patrol2.AD_CLOSE 是同一个点
RELOAD_BTN = (1105, 926)       # 断线弹窗的「重新载入游戏」（UI 固定坐标，不随镜头变）

# 掉到登录页就得停手：QQ / 微信授权只能用户本人做，脚本绝不尝试自己登。
LOGIN_WORDS = ("QQ登录", "微信登录", "授权登录", "登录游戏", "切换账号",
               "游客登录", "实名认证", "同意并继续")

# coc_patrol2.grab() 里用到的模块级计数器 _mc_fail 只在一帧成功取回之后才会被赋值，
# minicap 起不来时第一次 grab 会抛 NameError。这里先把它补齐：内存后端不能因为
# 抓帧层的小毛病整轮崩掉（minicap 不行就退回 screencap），coc_patrol2 本身不动。
if not hasattr(_ocr, "_mc_fail"):
    _ocr._mc_fail = 0

STATE = os.path.join(HERE, "coc_state.json")          # 资源地址 + 最近一次读数（定位种子）
COLLECTORS = os.path.join(HERE, "collectors_mem.json")  # 17 个采集点 + dismiss（标定视角）
CALIB = os.path.join(HERE, "mem_calib.json")          # 当前镜头变换缓存
CALIB_TRUSTED = os.path.join(HERE, "mem_calib_trusted.json")  # 最近一次「可信」位姿（只记可信的）
ANCHORS = os.path.join(HERE, "coc_templates", "village_anchors.npz")  # 村庄锚点图（定缩放）

TRUST_MIN_MATCHES = 5     # 锚点/气泡交叉验证命中这么多个才算「可信」
NEAR_MAX_PX = 40.0        # 「近似可信」：与上次可信位姿的平移差上限（像素）
NEAR_MAX_DS = 0.02        # 「近似可信」：与上次可信位姿的缩放差上限
NEAR_MAX_AGE_S = 7200     # 可信位姿有效期：超过 2 小时就重新找，别让偏差一路累加

MARK = "__COC_MEM__"
PAGE = 4096
BLK = 8 << 20            # 重定位时每块读 8MB
DD_TIMEOUT = 8           # 手机端 timeout：读到未映射页会永久阻塞，必须让远端自己超时
SCAN_BUDGET = 300        # 没给 deadline 时的单次搜索预算（秒，兜底用）

# ---- 时间预算：这四项由 coc_config 覆盖（village_wait_s / locate_stage_s /
# locate_total_s / round_max_s），默认值和配置默认值一致。定位是最容易卡住的一步：
# 三级搜索（精确 → 宽窗 → 结构兜底）如果每一级都跑到底，实测能烧掉十分钟以上。
# 所以每级加时限、整轮再加一个总时限，到点就放弃这一轮，绝不无限跑。
VILLAGE_WAIT = 120       # 冷启动后等进村的上限
LOCATE_STAGE = 180       # 单级定位的墙钟上限
LOCATE_TOTAL = 300       # 整轮定位的上限
ROUND_MAX = 600          # 一轮的硬上限（给 coc_patrol_run 的看门狗用）

# 最近一次 relocate() 的结果（给 meta.json 看「为什么没定上位置」）
LOCATE_INFO = {}
_STEPS = []


def set_budgets(cfg=None):
    """把 config 里的时间预算应用到模块常量。返回生效的四元组（方便写日志）。"""
    global VILLAGE_WAIT, LOCATE_STAGE, LOCATE_TOTAL, ROUND_MAX
    cfg = cfg or {}
    for name in ("VILLAGE_WAIT", "LOCATE_STAGE", "LOCATE_TOTAL", "ROUND_MAX"):
        v = cfg.get(name.lower())
        if isinstance(v, (int, float)) and v > 0:
            globals()[name] = int(v)
    return dict(village_wait_s=VILLAGE_WAIT, locate_stage_s=LOCATE_STAGE,
                locate_total_s=LOCATE_TOTAL, round_max_s=ROUND_MAX)

# 重定位只扫这几类段：实测资源结构在 libc_malloc 里；其余段命中率低还费时间
SKIP_NAME = ("dalvik", "jit", "ashmem", "dev/", ".so", ".oat", ".vdex", ".art",
             "linker", "gralloc", "kgsl", "stack", "vectors", ".dex", ".apk")


# ---------------------------------------------------------------- adb 原语
def _adb(args, timeout=30, **kw):
    cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + list(args)
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, timeout=timeout, **kw)


def _shx(cmd, timeout=20):
    """跑一条 adb shell（shell 用户，不走常驻 root shell）。

    ⚠ `am` / `wm` / `monkey` 绝对不能走常驻 root shell：su 域里调这几个会报
    「Failure calling service activity: Failed transaction (2147483646)」——
    命令返回码 0、看起来执行了，实际一件事都没做（force-stop 尤其致命：
    游戏一直挂着，屏幕亮一晚上）。实测只有 shell 用户才能正常 force-stop。
    读内存/点击（input）用 root shell 没问题，这两类继续走 shell()。
    """
    try:
        r = _adb(["shell", cmd], timeout=timeout, text=True)
        return (r.stdout or "").strip()
    except Exception as e:
        log(f"  adb shell 失败（{cmd[:40]}）：{type(e).__name__}: {e}")
        return ""


def find_pid():
    """游戏进程 pid。读不到抛 RuntimeError。"""
    out = _adb(["shell", f"ps -A | grep {PKG}"], timeout=20, text=True).stdout
    for line in out.splitlines():
        f = line.split()
        if len(f) > 1 and PKG in line:
            try:
                return int(f[1])
            except ValueError:
                continue
    raise RuntimeError("游戏进程没找到（没启动或已退出）")


class RootShell:
    """常驻 root shell：`adb shell` 里挂一个 `su -c 'sh'`，之后每条命令 ~70ms。

    绝对不要用 `adb exec-out su -c dd` 读内存：会往二进制里插字节（8192→8195），
    模式匹配全空。要么常驻 shell 里 `dd | od`，要么 dd 到手机文件再 pull。
    """

    def __init__(self, pid=None):
        self.p = None
        self.pid = pid
        self.open()

    def open(self):
        self.close()
        cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + ["shell"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=0)
        self.p.stdin.write(b"su -c 'sh'\n")
        self.p.stdin.flush()
        time.sleep(1.2)
        self.drain(0.4)
        if not self.pid:
            try:
                self.pid = find_pid()
            except RuntimeError:
                self.pid = None

    def close(self):
        if self.p:
            try:
                self.p.stdin.write(b"exit\nexit\n")
                self.p.stdin.flush()
            except Exception:
                pass
            try:
                self.p.terminate()
            except Exception:
                pass
            self.p = None

    def drain(self, t=0.3):
        out = b""
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([self.p.stdout], [], [], 0.02)
            if r:
                c = os.read(self.p.stdout.fileno(), 65536)
                if not c:
                    break
                out += c
        return out

    def run(self, cmd, timeout=8.0):
        if self.p is None:
            self.open()
        try:
            self.p.stdin.write(f"{cmd}; echo {MARK}\n".encode())
            self.p.stdin.flush()
        except Exception:
            self.open()
            self.p.stdin.write(f"{cmd}; echo {MARK}\n".encode())
            self.p.stdin.flush()
        buf = b""
        t0 = time.time()
        while MARK.encode() not in buf:
            r, _, _ = select.select([self.p.stdout], [], [], 0.05)
            if r:
                chunk = os.read(self.p.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                buf += chunk
            if time.time() - t0 > timeout:
                break
        s = buf.decode("utf-8", "replace").replace("\r", "")
        i = s.find(MARK)
        return s[:i] if i >= 0 else s

    # ---- 读内存 ----
    def read_words(self, addr, n=4):
        a = int(addr) & ~3
        out = self.run(f"dd if=/proc/{self.pid}/mem bs=4 skip={a // 4} count={n} "
                       f"2>/dev/null | od -An -tu4", timeout=6)
        nums = [int(x) for x in out.split() if x.isdigit()]
        return nums[:n] if len(nums) >= n else None

    def read_bytes(self, va, size):
        """读任意一段内存（base64 传回，32KB 约 80ms）。失败返回 b""

        dd 的 skip 以页为单位：先对齐页首，读回来再切掉页内偏移。
        绝对不要用 `dd | od` 读大块：od 会按字节插空格，二进制就废了。
        """
        if size <= 0:
            return b""
        page = int(va) & ~0xFFF
        delta = int(va) - page
        need = delta + int(size)
        out = self.run(f"dd if=/proc/{self.pid}/mem bs=4096 skip={page // 4096} "
                       f"count={(need + 4095) // 4096} 2>/dev/null | base64 -w0",
                       timeout=30)
        try:
            b = base64.b64decode("".join(out.split()))
        except Exception:
            return b""
        return b[delta:delta + size] if len(b) >= need else b[delta:]

    def alive(self):
        return self.pid and "YES" in self.run(f"test -d /proc/{self.pid} && echo YES", timeout=4)

    # ---- 输入 ----
    def tap(self, x, y, wait=0.38):
        self.run(f"input tap {int(x)} {int(y)}")
        if wait:
            time.sleep(wait)


_shell = None


def shell():
    global _shell
    if _shell is None or _shell.p is None:
        _shell = RootShell()
    return _shell


def close():
    """释放常驻 root shell（脚本收尾用）。"""
    global _shell
    if _shell is not None:
        _shell.close()
        _shell = None


# ---------------------------------------------------------------- 屏幕 / 前台
def power_state():
    """亮屏/熄屏状态：mWakefulness=Awake/Asleep/Dozing + Display Power: state=ON/OFF。"""
    txt = _shx('dumpsys power | grep -E "mWakefulness=|Display Power"', timeout=15)
    wake, disp = None, None
    for line in txt.splitlines():
        line = line.strip()
        if "mWakefulness=" in line and "Changing" not in line:
            wake = line.split("mWakefulness=", 1)[1].strip()
        if "Display Power" in line:
            disp = line.split("state=", 1)[1].strip() if "state=" in line else line
    return dict(wakefulness=wake, display=disp,
                awake=(wake == "Awake"), asleep=(wake in ("Asleep", "Dozing")),
                raw=" | ".join(l.strip() for l in txt.splitlines() if l.strip())[:200])


def awake():
    """屏幕是不是亮着（KEYCODE_SLEEP 之后 dumpsys 里是 Asleep/Dozing）。"""
    return power_state()["awake"]


def keyguard_shown():
    """锁屏还在不在。返回 True / False / None（这版 ROM 里看不到判据）。

    唤醒 ≠ 回到 App：实测设了 stayon 也会因为系统超时熄屏，唤醒后停在滑动锁屏上，
    那一帧上认什么都没用。所以锁屏必须单独判、单独解，不能靠“亮度够了”就以为回到村庄。
    """
    out = _shx("dumpsys window | grep -E "
               "'mShowingLockscreen=|mDreamingLockscreen=|isStatusBarKeyguard='",
               timeout=15).lower()
    # 实测这版 ROM 打的是 isStatusBarKeyguard=true/false（不是 isShown），
    # 两个都认，免得刷机/升级后默默退化成「永远判不出来」
    if ("isstatusbarkeyguard=true" in out or "isshown=true" in out
            or "mshowinglockscreen=true" in out or "mdreaminglockscreen=true" in out):
        return True
    if ("isstatusbarkeyguard=false" in out or "isshown=false" in out
            or "mshowinglockscreen=false" in out):
        return False
    return None


def wake_unlock():
    """点亮屏幕 + 解锁。返回 {awake, woke, keyguard, unlock, taps}。

    顺序：KEYCODE_WAKEUP → wm dismiss-keyguard → 还停在锁屏就上滑一次再来。
    上滑坐标取屏幕中下部（2220×1080）到中上部，速度要快（200ms），慢了会被当成长按。
    """
    s = shell()
    out = dict(woke=False, awake=False, keyguard=None, unlock=None, taps=0)
    if not awake():
        _shx("input keyevent KEYCODE_WAKEUP", timeout=15)
        out["woke"] = True
        time.sleep(1.0)
    out["awake"] = awake()
    if not out["awake"]:
        return out
    out["keyguard"] = keyguard_shown()
    for _ in range(2):
        # wm 走 shell 用户：root 域里调 wm 会 Failed transaction，锁屏根本解不开
        _shx("wm dismiss-keyguard", timeout=15)
        time.sleep(0.9)
        kg = keyguard_shown()
        out["keyguard"] = kg
        if kg is False or PKG in focus():
            break
        _shx("input swipe 1110 900 1110 300 200", timeout=15)   # 上滑解锁
        out["taps"] += 1
        time.sleep(0.9)
    out["unlock"] = (keyguard_shown() is not True)
    out["awake"] = awake()
    log(f"  屏幕 wake={out['woke']} awake={out['awake']} keyguard={out['keyguard']} "
        f"unlock={out['unlock']}")
    return out


def focus():
    """当前前台窗口。

    注意：dumpsys window 里 mCurrentFocus 会先打印一行 null 再打印真实值，
    用 -m1 会永远拿到 null，导致每轮都误判「游戏不在前台」而去拉起 App（会白等 40s）。
    """
    return shell().run("dumpsys window | grep mCurrentFocus= | tail -2")


def ensure_ready():
    """确保 屏幕亮 + 游戏在前台 + pid 有效。返回 (changed, [原因])。

    只做「补缺失的那一项」，不重启游戏（老接口，保留给不想冷启动的场景）。
    需要完整的「唤醒→解锁→拉起→等进村」时用 start_round()。
    """
    s = shell()
    reasons = []
    if not awake():
        s.run("input keyevent KEYCODE_WAKEUP")
        time.sleep(0.9)
        reasons.append("wakeup")
    if PKG not in focus():
        if keyguard_shown():
            wake_unlock()
            reasons.append("unlock")
        _shx(f"am start -n {PKG}/{ACT}", timeout=20)
        time.sleep(3.5)
        reasons.append("relaunch")
    if not s.alive():
        try:
            s.pid = find_pid()
        except RuntimeError as e:
            raise RuntimeError(f"游戏进程不在，也拉不起来：{e}")
        reasons.append("pid=%d" % s.pid)
    return bool(reasons), reasons


# ---------------------------------------------------------------- 一轮的开始 / 结束
def pid_of():
    """游戏进程还在不在（force-stop 之后要能确认它真的退了）。

    用 pidof 而不是 ps|grep：后者会把自己的 shell 和 grep 也算进去。
    """
    try:
        out = _adb(["shell", f"pidof {PKG}"], timeout=15, text=True).stdout.strip()
    except Exception:
        return None
    for tok in out.split():
        if tok.isdigit():
            return int(tok)
    return None


def login_page(joined):
    """画面里是不是登录 / 授权页。命中就不碰，直接放弃这一轮（QQ/微信只能用户本人登）。"""
    if not joined:
        return False
    if any(w in joined for w in LOGIN_WORDS):
        return True
    return "登录" in joined and ("QQ" in joined or "微信" in joined)


def _village_ok(tx):
    """比 coc_patrol2.classify 宽松一点的村庄判据（只给内存后端用，不动公共原语）。

    classify 要求「y<100 处有一个 ≥5 位的数字」——但 OCR 会把 3 270 907 拆成
    '3270' + '907'（实测 11-23 21:00 那轮就是这样），classify 于是判成 unknown，
    冷启动眼睁睁等满 120s 判失败，而画面明明就是干净村庄（fail.jpg 为证）。
    这里把右上角资源区那几段数字**按位置拼起来再数位数**，其余判据（底栏「建造/战斗」）
    和 classify 完全一致。

    资源区取 y<250（金币/圣水/暗黑是三行，y≈57/131/205）：原先只看到 y<100 那一行
    金币的数字，而金币那行恰好是活动高光/描边最花的一行，OCR 会认成 'SES 6E0' 这种乱码
    （实测 11-26 15:49 那轮，圣水那行明明读出了 7 位 '9889659' 在第 131 行，却没被看到），
    结果就是人在村里、底栏两字都在，还硬等超时。
    """
    if not tx:
        return False
    bottom = [t["t"] for t in tx if t["y"] > 940]
    if not any("建造" in t or "战斗" in t for t in bottom):
        return False
    digits = 0
    for t in tx:
        if t["y"] < 250 and 1600 <= t["x"] <= 2220:
            digits += len("".join(c for c in t["t"] if c.isdigit()))
    return digits >= 5


def village_probe(img=None):
    """画面里是什么。返回 (kind, joined_text)，kind ∈ village/loading/reconnect/login/
    ad/popup/quit_confirm/panel/unknown/noimg。

    判据直接用 coc_patrol2 的 classify（底栏「建造/战斗」+ 右上角 5 位数字 = 资源条），
    它已经在 OCR 后端跑了很久；这里只把它的结论翻译成「该等 / 该点 / 该放弃」。
    唯一的补充：classify 说 unknown/panel、但底栏和资源数字都在时，按 _village_ok 认成村庄
    （数字被 OCR 拆段的情况，见 _village_ok）。
    """
    if img is None:
        img = grab()
    if img is None:
        return "noimg", ""
    try:
        mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    except Exception:
        mean = 0.0
    try:
        tx = _ocr.ocr_all(img)
    except Exception:
        tx = []
    joined = " ".join(t.get("t", "") for t in tx)
    if login_page(joined):
        return "login", joined
    if mean < 45 and not any(len(t.get("t", "")) >= 2 for t in tx):
        return "loading", joined          # 全黑的载入页：不浪费 OCR/点击，接着等
    kind = _ocr.classify(tx)
    if kind in ("unknown", "panel") and _village_ok(tx):
        kind = "village"
    return kind, joined


def screen_kind(img=None):
    """给「登录态巡检」用的统一画面判据（两个后端同名同签名）。

    返回 dict(kind, text, brightness)：
      kind ∈ village / loading / reconnect / login / ad / popup / quit_confirm / panel / unknown / noimg
    这里只读不点，也不做定位：检查登录态时绝不能去 relocate（那是巡检的活）。
    """
    if img is None:
        img = grab()
    bright = None
    if img is not None:
        try:
            bright = round(float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()), 1)
        except Exception:
            bright = None
    kind, joined = village_probe(img)
    return dict(kind=kind, text=joined, brightness=bright)


MAX_POPUPS = 6          # 一轮冷启动最多关 6 个弹窗（关完还有就认输，别死循环）
MAX_CLICKS = 8          # 一轮冷启动最多点 8 下（含关弹窗、点空地收面板）


def _frame_diff(a, b):
    """两帧差多少（灰度平均绝对差）。判断「点这一下画面到底变没变」。"""
    if a is None or b is None or a.shape != b.shape:
        return 999.0
    try:
        return float(cv2.absdiff(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY),
                                 cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)).mean())
    except Exception:
        return 999.0


def _clicks_str(clicks):
    return "".join(f"({x},{y})" for x, y in clicks) or "无"


def cold_start(wait=None, launch=True, fail_path=None):
    """一轮的开始：唤醒解锁 → 没跑就拉起 → 轮询等进村 → 关掉挡屏的弹窗/面板。

    返回 dict(ok, mode, woke, unlocked, launched, pid, waited, via, steps, error,
              taps, last_kind, fail_screen)

    为什么不等固定秒数：刚拉起的游戏 18~40s 才进村（看机器忙不忙），死等 25s 要么白等
    要么早读；这里按画面判（村庄锚点 / 资源条读得出来），进村就往下走，最多等
    VILLAGE_WAIT 秒，到点就交回失败——绝不能把一轮拖成无限长。

    挡屏的东西按「能不能认出关闭键」处理（判据全在 coc_patrol2.blocker）：
      白 ✕ / 红 ✕ / 对话框 ✕ → 点那个 ✕（只点右上角，绝不点「前往查看」「领取」）
      左侧竖面板           → 点空地收掉（点面板右沿之外的空地，点面板里等于没点）
      提示气泡             → 点一次空地，还在就不管了（它不挡按钮，只可能压住一个采集点）
      整屏压暗但认不出 ✕   → 依次试候选角点，点完看画面变没变，变了就停
    实测（19.100.1）：关掉「欢迎来到公共频道」之后，游戏会自己打开左侧部落信息面板、
    右下角再弹一个女王提示气泡，所以关完弹窗必须再收一次尾，否则进不了干净村庄。

    红线：掉到 QQ/微信 登录页就写 alert 退出，绝不自己登。
    """
    t0 = time.time()
    wait = float(wait or VILLAGE_WAIT)
    out = dict(ok=False, mode=None, woke=False, unlocked=None, launched=False,
               pid=None, waited=0.0, via=None, steps=[], error=None,
               taps=[], last_kind=None, fail_screen=None)

    w = wake_unlock()
    out["woke"], out["unlocked"] = w.get("woke"), w.get("unlock")
    if not w.get("awake"):
        out["error"] = "screen_off（唤醒后屏幕仍是熄的）"
        return out

    pid = pid_of()
    if pid:
        # 进程还在（上一轮异常没关成、或用户自己开着）：只叫到前台，不重启、不等进村，
        # 地址可能还有效，省一次两三分钟的定位。
        out["mode"] = "warm"
        out["pid"] = pid
        shell().pid = pid
        if PKG not in focus():
            _shx(f"am start -n {PKG}/{ACT}", timeout=20)
            out["steps"].append("bring_to_front")
            time.sleep(3.0)
        out["ok"] = True
        out["via"] = "running"
        out["waited"] = round(time.time() - t0, 1)
        log(f"  游戏已在跑（pid {pid}），只切换到前台（{out['waited']}s）")
        return out

    out["mode"] = "cold"
    if not launch:
        out["error"] = "game_not_running"
        return out

    log("  冷启动：拉起游戏")
    res = _shx(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1", timeout=25)
    out["launch_out"] = res[:120]
    out["launched"] = True
    out["steps"].append("launch")
    for _ in range(15):                      # 等进程起来，最多 15s
        time.sleep(1.0)
        pid = pid_of()
        if pid:
            break
    if not pid:
        out["error"] = "launch_failed（monkey 拉起后也没有进程）"
        return out
    out["pid"] = pid
    shell().pid = pid

    # 轮询等进村：画面说了算，最多 VILLAGE_WAIT 秒
    deadline = time.time() + wait
    reloads = 0
    pops = 0                     # 关掉的弹窗个数
    clicks = []                  # 点过的坐标（失败时写进 error / fail_screen，方便复盘）
    shade_idx = 0                # 「压暗但认不出 ✕」时按候选点依次试
    bubble_tried = False         # 气泡只点一次空地，之后不管它
    promo_tried = False          # 「压暗 + 推广文案」兜底只试一次
    last_kind = None
    img = None
    while time.time() < deadline:
        img = grab()
        kind, joined = village_probe(img)
        last_kind = kind
        if kind == "login":
            out["error"] = "login_page（掉到登录/授权页，停手等人工处理）"
            out["steps"].append("login")
            log(f"  ⚠ 画面出现登录页：{joined[:80]}")
            break
        if kind == "reconnect":
            if reloads >= 3:
                out["error"] = "reconnect_loop（连点 3 次重新载入还是断线）"
                break
            log(f"  断线弹窗，点「重新载入游戏」@{RELOAD_BTN}")
            shell().tap(*RELOAD_BTN, wait=1.5)
            reloads += 1
            out["steps"].append("reload")
            deadline = time.time() + wait        # 重载要重新计时（但总时长仍有看门狗）
            time.sleep(6.0)
            continue

        blk = _ocr.blocker(img, ignore=("bubble",) if bubble_tried else ())
        if blk and blk["kind"] in ("white-x", "red-x", "dialog", "shade"):
            if pops >= MAX_POPUPS or len(clicks) >= MAX_CLICKS:
                out["error"] = (f"popup_loop（关了 {pops} 个弹窗还没进村，"
                                f"最后画面 {last_kind}）")
                break
            if blk["kind"] == "shade":
                # 认不出 ✕：按候选点依次试，点完看画面变没变，变了就从头再判
                cands = _ocr.close_candidates(img)
                x, y, why = cands[min(shade_idx, len(cands) - 1)]
                shade_idx += 1
                log(f"  画面被压暗（暗区 {blk.get('dark_frac')}），试关弹窗点 {why} @({x},{y})")
                shell().tap(x, y, wait=1.5)
                clicks.append([x, y])
                pops += 1
                out["steps"].append(f"modal_tap(shade:{why})")
                after = grab()
                if _frame_diff(img, after) < 3.0:
                    log("    画面没变，换下一个候选点")
                else:
                    shade_idx = 0
                continue
            log(f"  关弹窗（{blk['kind']}）@({blk['x']},{blk['y']})")
            shell().tap(blk["x"], blk["y"], wait=1.5)
            clicks.append([blk["x"], blk["y"]])
            pops += 1
            out["steps"].append(f"popup_closed({blk['kind']})")
            # 关完不睡觉，直接回循环头再判一帧：这一步就是在做「二次收尾」
            continue

        if blk and blk["kind"] == "sidebar":
            if len(clicks) >= MAX_CLICKS:
                out["error"] = (f"panel_stuck（左侧面板点了 {len(clicks)} 下还没收掉，"
                                f"最后画面 {last_kind}）")
                break
            log(f"  左侧面板开着（竖边 x={blk.get('edge')}），点空地收掉 @({blk['x']},{blk['y']})")
            shell().tap(blk["x"], blk["y"], wait=1.2)
            clicks.append([blk["x"], blk["y"]])
            out["steps"].append("panel_dismissed")
            continue

        if blk and blk["kind"] == "bubble":
            bubble_tried = True
            if len(clicks) < MAX_CLICKS:
                log(f"  有个提示气泡，点空地试探 @({blk['x']},{blk['y']})")
                shell().tap(blk["x"], blk["y"], wait=1.0)
                clicks.append([blk["x"], blk["y"]])
                out["steps"].append("bubble_dismissed")
                continue
            continue

        if kind == "popup" and any(w in joined for w in _ocr.POPUP_WORDS):
            # 「欢迎回来」「村庄被袭击」这类：文案对得上才点「确定」
            # （和 enter_village 同一条红线：绝不盲点确定，兜底只碰右上角的 ✕）
            if pops >= MAX_POPUPS or len(clicks) >= MAX_CLICKS:
                out["error"] = (f"popup_loop（关了 {pops} 个弹窗还没进村，"
                                f"最后画面 {last_kind}）")
                break
            b = _ocr.find_btn(_ocr.ocr_all(img), ("确定", "好的", "继续"))
            if b:
                log(f"  关欢迎弹窗 {b['t']!r} @({b['x']},{b['y']})")
                shell().tap(b["x"], b["y"], wait=1.5)
                clicks.append([b["x"], b["y"]])
                pops += 1
                out["steps"].append("popup_closed(welcome)")
                continue

        # 兜底：认不出形状、又不是村庄，就用「压暗 + 推广文案」那条老路试一次
        # （全屏活动页背景很亮时 modal_shade 不成立，只有这条能认出来）
        if kind in ("ad", "popup", "unknown", "panel") and not promo_tried and len(clicks) < MAX_CLICKS:
            promo_tried = True
            try:
                dim = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) if img is not None else 255.0
            except Exception:
                dim = 255.0
            if dim >= 95:
                # dismiss_promo 只在「压暗 + 推广文案」同时成立时才点；画面不暗就不会点，
                # 那就别把这一步记进 steps（否则日志里会多一条没发生过的动作）
                pass
            else:
                try:
                    after = dismiss_promo(img)
                except Exception:
                    after = None
                if after is not None and _frame_diff(img, after) > 3.0:
                    out["steps"].append("popup_closed(promo)")
                    log("  关掉全屏弹层（压暗 + 推广文案），画面已变")
                    continue

        if kind == "village":
            time.sleep(0.8)                  # 进村那一两秒数字还在滚动，稳一下
            img2 = grab()
            kind2, _ = village_probe(img2)
            blk2 = _ocr.blocker(img2, ignore=("bubble",) if bubble_tried else ())
            if kind2 == "village" and blk2 is None:
                out["ok"] = True
                out["via"] = "village"
                out["steps"].append("village")
                img = img2
                break
            img = img2
            continue

        if time.time() >= deadline - 4:
            break
        time.sleep(2.0)

    out["taps"] = clicks
    out["last_kind"] = last_kind
    if not out["ok"] and not out["error"]:
        out["error"] = (f"village_timeout（{wait:.0f}s 内没进村，最后画面 {last_kind}，"
                        f"点过 {_clicks_str(clicks)}）")
    if not out["ok"]:
        # 留证据：失败这一帧存下来，下次出问题能对着图复盘（以前只有 meta.json，
        # 一张图都没有，只能靠猜）
        out["fail_screen"] = dict(kind=last_kind, taps=[list(c) for c in clicks],
                                  steps=list(out["steps"]))
        if fail_path:
            try:
                if img is not None:
                    os.makedirs(os.path.dirname(fail_path) or ".", exist_ok=True)
                    cv2.imwrite(fail_path, img)
                    out["fail_screen"]["path"] = os.path.basename(fail_path)
                    out["fail_screen"]["bright"] = round(
                        float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()), 1)
            except Exception as e:
                out["fail_screen"]["error"] = f"{type(e).__name__}: {e}"
    out["waited"] = round(time.time() - t0, 1)
    log(f"  冷启动 {'成功' if out['ok'] else '失败'} {out['waited']}s via={out['via']} "
        f"steps={'/'.join(out['steps'])} err={out['error']}")
    return out


def start_round(cold=True, wait=None, fail_path=None):
    """一轮开头的统一入口（取代老的 ensure_ready + enter_village 两步）。

    fail_path：进村失败时把最后一帧存到这里（调用方给 history/<id>/fail.jpg）。
    """
    out = cold_start(wait=wait, launch=True, fail_path=fail_path)
    if not out["ok"] and out.get("mode") == "cold" and not cold:
        out["error"] = out["error"] or "game_not_running"
    if out["ok"] and out.get("mode") == "cold":
        # 新进程 = 老地址一定失效。主动标记失效（保留 seed）比等 read_all 撞一遍快。
        invalidate("cold_start")
    return out


def close_game(timeout=12):
    """杀掉游戏，回读 pidof 确认真的退了。返回 dict(ok, pid, sec, out)。

    force-stop 必须走 shell 用户（_shx）：root 域里调 am 会 Failed transaction，
    看上去执行了实际什么也没做（实测游戏一直活着）。
    """
    t0 = time.time()
    out_s = _shx(f"am force-stop {PKG}", timeout=25)
    time.sleep(1.0)
    pid = pid_of()
    while pid and time.time() - t0 < timeout:
        time.sleep(1.0)
        pid = pid_of()
    out = dict(ok=not pid, pid=pid, sec=round(time.time() - t0, 1), out=out_s[:120])
    log(f"  关掉游戏 {'✓' if out['ok'] else '✗'}（{out['sec']}s，pid={pid}）")
    return out


def sleep_screen(tries=2):
    """锁屏：KEYCODE_SLEEP 之后读 dumpsys 确认 mWakefulness=Asleep。

    为什么要回读：stayon/svc power stayon true 这类设置会让「息屏」静默失效，
    不读一遍 dumpsys 就不知道屏幕其实还亮着（那就是白烧一晚上屏幕）。
    KEYCODE_SLEEP 不生效时用电源键 26 兜底。
    """
    t0 = time.time()
    st = power_state()
    for i in range(max(1, tries)):
        key = "KEYCODE_SLEEP" if i == 0 else "26"
        _shx(f"input keyevent {key}", timeout=15)
        time.sleep(1.4)
        st = power_state()
        if st["asleep"]:
            break
    ok = bool(st["asleep"])
    out = dict(ok=ok, wakefulness=st["wakefulness"], display=st["display"],
               sec=round(time.time() - t0, 1))
    log(f"  锁屏 {'✓' if ok else '✗'}（{out['wakefulness']} / Display {out['display']}）")
    return out


def shutdown(do_close=True, do_lock=True):
    """一轮干完之后的收尾：杀游戏 + 锁屏。任何一步失败都不抛异常，只写日志。

    调用时机很关键：必须在 meta.json 落盘之后（否则先杀游戏会导致截图/后读拿不到）。
    """
    out = dict(close=None, lock=None, ok=True)
    if do_close:
        try:
            out["close"] = close_game()
        except Exception as e:
            out["close"] = dict(ok=False, error=f"{type(e).__name__}: {e}")
        out["ok"] = out["ok"] and bool(out["close"].get("ok"))
    if do_lock:
        try:
            out["lock"] = sleep_screen()
        except Exception as e:
            out["lock"] = dict(ok=False, error=f"{type(e).__name__}: {e}")
        out["ok"] = out["ok"] and bool(out["lock"].get("ok"))
    return out


def screencap(path):
    """截一屏到本地（PNG）。失败返回 None。"""
    try:
        with open(path, "wb") as f:
            r = _adb(["exec-out", "screencap", "-p"], timeout=30, stdout=f,
                     capture_output=False)
        if getattr(r, "returncode", 0) != 0:
            return None
    except Exception:
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None


# ---------------------------------------------------------------- 状态文件
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def load_calib():
    try:
        with open(CALIB) as f:
            return json.load(f)
    except Exception:
        return None


def save_calib(t):
    tmp = CALIB + ".tmp"
    with open(tmp, "w") as f:
        json.dump(t, f, indent=1)
    os.replace(tmp, CALIB)


def load_trusted():
    """最近一次可信位姿（只在判为可信时写，不会被低命中的结果污染）。"""
    try:
        with open(CALIB_TRUSTED) as f:
            return json.load(f)
    except Exception:
        return None


def save_trusted(t):
    tmp = CALIB_TRUSTED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(t, f, indent=1)
    os.replace(tmp, CALIB_TRUSTED)


def load_collectors():
    with open(COLLECTORS) as f:
        return json.load(f)


# ---------------------------------------------------------------- 读数
def sane(a):
    """读数像不像一个真实资源结构（按 TH15 的仓库上限给一倍余量）。

    以前四个字段全放到 2 亿，等于不设防：实测一个假地址读出 gems=17,664（实际 349）、
    dark=8,750,336（暗黑上限 30 万，差 30 倍），因为 sane 放它过去，整轮升级点位全乱、
    研究 8 次全失败（2026-11-25 01:53 那轮，地址 0x6f00e5c128）。
    暗黑是最硬的判据：它上限最低、正常值最小，假结构基本过不了。
    """
    if not a:
        return False
    if not all(0 <= a[k] <= 30_000_000 for k in ("gems", "gold", "elixir")):
        return False
    if not 0 <= a["dark"] <= 600_000:
        return False
    if a["gold"] == 0 and a["elixir"] == 0:
        return False
    if a["gems"] > 100_000:
        return False
    return True


def read_all(retries=1, allow_locate=True):
    """返回 {gems, gold, elixir, dark} 或 None。地址失效自动重定位。"""
    st = load_state()
    if not st.get("gold"):
        if not allow_locate:
            return None
        if not relocate():
            return None
        st = load_state()
    for i in range(retries + 1):
        s = shell()
        if not s.pid:
            try:
                s.pid = find_pid()
            except RuntimeError:
                return None
        if st.get("pid") and st["pid"] != s.pid:
            # 进程换了 = 地址一定失效，别拿旧地址瞎读（读到的可能是别人的数据）
            log(f"进程变了 {st['pid']} → {s.pid}，重新定位地址")
            if not allow_locate or not relocate():
                return None
            st = load_state()
            allow_locate = False
        base = st.get("gems_addr") or (st["gold"] - 4)
        nums = s.read_words(base, 4)
        if nums and len(nums) >= 4:
            v = dict(gems=nums[0], gold=nums[1], elixir=nums[2], dark=nums[3])
            if sane(v):
                return v
        if i < retries:
            time.sleep(0.2)
    if allow_locate and relocate():
        return read_all(retries=0, allow_locate=False)
    return None


def read_res(img=None):
    """读三项资源。img 参数只为和 OCR 后端保持函数签名一致（内存模式不看画面）。"""
    a = read_all()
    if not a:
        return {"gold": None, "elixir": None, "dark": None}
    return {"gold": a["gold"], "elixir": a["elixir"], "dark": a["dark"]}


def read_res_vote(n=3):
    """内存读数本身逐位精确，不需要投票：读一次，不合法就重试，最多 n 次。"""
    out = {"gold": None, "elixir": None, "dark": None}
    for _ in range(max(1, n)):
        a = read_all()
        if a:
            out = {"gold": a["gold"], "elixir": a["elixir"], "dark": a["dark"]}
            remember_sample(out)          # 顺手把种子刷成最新读数
            break
        time.sleep(0.2)
    log(f"内存读数 {out}（来源 mem）")
    return out


def read_res_until(n=3, tries=4, gap=1.2):
    best = {"gold": None, "elixir": None, "dark": None}
    for _ in range(max(1, tries)):
        r = read_res_vote(n)
        for k in best:
            if best[k] is None and r.get(k) is not None:
                best[k] = r[k]
        if best["gold"] is not None and best["elixir"] is not None:
            break
        time.sleep(gap)
    return best


def read_res_stable(prev=None, tag="_res", tries=3):
    """和 prev 比骤降 >20% 就再读几帧取最大 —— 内存读数不该骤降，骤降说明读到了脏地址。"""
    best = read_res_vote(2)
    if not prev:
        return best
    for _ in range(tries):
        bad = [k for k in best if prev.get(k) and
               (best[k] is None or best[k] < prev[k] * 0.8)]
        if not bad:
            break
        alt = read_res_vote(1)
        for k in bad:
            if alt.get(k) and (best[k] is None or alt[k] > best[k]):
                best[k] = alt[k]
        time.sleep(0.3)
    return best


def read_workers(img=None):
    """空闲/总工人在内存里还没定位（这是 UI 文本，不在资源结构里），降级走 OCR。"""
    if img is None:
        img = grab()
    a, b = _ocr_read_workers(img)
    log(f"工人数 {a}/{b}（来源 ocr，内存未定位）")
    return a, b


def read_star(img=None):
    """胜利之星是左下角五颗星的亮/暗，内存里也没定位，降级走 OCR 的图像判据。"""
    if img is None:
        img = grab()
    n = _ocr_read_star(img)
    log(f"胜利之星 {n}/5（来源 ocr，内存未定位）")
    return n


# ---------------------------------------------------------------- 抓帧
_ocr_grab = _ocr.grab          # 原来的抓帧：minicap 流，挂了它自己退 screencap


def grab(tag=None):
    """取一帧：minicap → screencap 兜底，两条都不行返回 None。

    minicap 偶尔起不来（上一次的守护进程没退干净、投影尺寸变了……），
    `coc_patrol2.grab()` 在这种情况下会直接抛异常。内存模式下把这一层接过来，
    面板守卫/调试截图用同一份画面，抓帧出一点毛病也不会把整轮带崩。
    """
    try:
        img = _ocr_grab(tag)
    except Exception as e:
        log(f"  minicap 抓帧失败，退 screencap：{type(e).__name__}: {e}")
        img = None
    if img is not None and getattr(img, "size", 0):
        return img
    p = f"/tmp/{tag or '_cur'}.png"
    try:
        with open(p, "wb") as f:
            _adb(["exec-out", "screencap", "-p"], timeout=30, stdout=f,
                 capture_output=False)
        img = cv2.imread(p)
    except Exception:
        return None
    return img if img is not None and img.size else None


# 内存模式下 coc_patrol2 内部（面板守卫、气泡检测、调试截图）也用这个抓帧函数
_ocr.grab = grab


def _sh(cmd, timeout=25):
    """接替 coc_patrol2.sh：它把 serial 写死进命令行（["adb", "-s", SERIAL, ...]）。

    SERIAL 为空（ANDROID_SERIAL 没设、只有一台设备）时会退化成 `adb -s '' shell ...`，
    adb 直接回 "error: device '' not found"，rc=1 但**不抛异常**。后果是面板守卫、
    点空地关面板、进村流程里的所有点击都静默失效（读数正常、点了没反应，最难查）。
    这里改成：有 serial 就带，没有就不带，交给 adb 自己挑唯一的设备。
    """
    return _adb(["shell", cmd], timeout=timeout)


_ocr.sh = _sh
sh = _sh          # 本模块自己的 sh 也换成带守卫的版本（coc_patrol_run 从这儿 import）


# ---------------------------------------------------------------- 重定位
def maps_raw(pid):
    """解析 /proc/<pid>/maps → [{start,end,size,perm,off,name}]（不过滤，按起始地址排序）。

    重定位和建筑池定位都要用同一份映射表，所以解析只在这里做一次。
    """
    out = []
    code = shell().run(f"cat /proc/{pid}/maps", timeout=20)
    for line in code.splitlines():
        m = re.match(r"([0-9a-f]+)-([0-9a-f]+)\s+(\S\S\S\S)\s+([0-9a-f]+)\s+\S+\s+\d+\s*(.*)",
                     line)
        if not m:
            continue
        start, end = int(m.group(1), 16), int(m.group(2), 16)
        out.append({"start": start, "end": end, "size": end - start,
                    "perm": m.group(3), "off": int(m.group(4), 16),
                    "name": m.group(5).strip()})
    out.sort(key=lambda r: r["start"])
    return out


def _segments(pid, min_mb=4):
    """重定位专用：只留「资源结构可能出现」的大块 rw-p 段，按大小降序。"""
    out = []
    for r in maps_raw(pid):
        if r["perm"] != "rw-p":
            continue
        name = r["name"]
        if not (name == "" or "libc_malloc" in name):
            continue
        if any(k in name for k in SKIP_NAME):
            continue
        if r["size"] < min_mb << 20:
            continue
        out.append(dict(start=r["start"], end=r["end"], size=r["size"], name=name))
    out.sort(key=lambda r: -r["size"])
    return out


def _pull_block(pid, addr, size, timeout=None):
    """读一段内存到本地。<关键> 必须带手机端 timeout，否则未映射页会把 adb 通道堵死。

    timeout 给了就把两条 adb 调用各自压到剩余时间以内：单块读卡住时最多拖掉
    几十秒，不会把整轮定位的预算吃光（实测 dd 碰到坏页会把整块拖到超时）。

    临时文件名带本进程 pid：两个进程同时扫内存时（手动跑 + cron 撞车），共用
    /data/local/tmp/_cocscan.bin 会让双方都 pull 到别人写了一半的块，候选全变成垃圾。
    """
    size = (size // PAGE) * PAGE or PAGE
    skip, cnt = addr // PAGE, size // PAGE
    tag = os.getpid()
    remote = f"/data/local/tmp/_cocscan_{tag}.bin"
    local = f"/tmp/_cocscan_{tag}.bin"
    sh_t = 25 if timeout is None else max(4, min(25, int(timeout)))
    pull_t = 60 if timeout is None else max(4, min(60, int(timeout)))
    cmd = (f"timeout {DD_TIMEOUT} dd if=/proc/{pid}/mem of={remote} bs={PAGE} "
           f"skip={skip} count={cnt} 2>/dev/null; chmod 666 {remote}")
    try:
        _adb(["shell", f"su -c {cmd!r}"], timeout=sh_t)
    except subprocess.TimeoutExpired:
        return b""
    try:
        r = _adb(["pull", remote, local], timeout=pull_t)
    except subprocess.TimeoutExpired:
        return b""
    if r.returncode != 0:
        return b""
    try:
        with open(local, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _u32(d):
    n = len(d) // 4
    return np.frombuffer(d[:n * 4], dtype="<u4").astype(np.int64)


def _find_exact(d, vals):
    pat = np.array(vals, dtype=np.uint32).tobytes()
    out, i = [], d.find(pat)
    while i >= 0:
        out.append(i)
        i = d.find(pat, i + 1)
    return out


def _find_near(d, spec):
    """按「每项一个窗口」找候选：spec = [(参考值, 相对窗口) x 3]，参考值为 None 表示不约束。

    画面读到的值用 0.1% 的窗口（读数只有几秒旧，资源值不会漂），上一轮存下的值
    用 2%（中间可能被收取或升级改过）。窗口收紧是必要的：实测 2% 的宽窗口会把
    「elixir 比上限还高」的假结构放进来。
    """
    a = _u32(d)
    if len(a) < 8:
        return []
    lo, hi = [], []
    for v, tol in spec:
        if not v:
            lo.append(0)
            hi.append(200_000_000)
            continue
        w = max(2000, int(v * tol))
        lo.append(max(0, v - w))
        hi.append(v + w)
    c = ((a[0:-3] >= lo[0]) & (a[0:-3] <= hi[0])
         & (a[1:-2] >= lo[1]) & (a[1:-2] <= hi[1])
         & (a[2:-1] >= lo[2]) & (a[2:-1] <= hi[2]))
    return [int(i) * 4 for i in np.nonzero(c)[0]]


def _find_shape(d):
    """结构搜索：gems 很小、gold/elixir/dark 互不相等、dark 后两个 word 相等。

    约束刻意收紧，否则数据表（1000/3600 这类）会成片命中。
    """
    a = _u32(d)
    if len(a) < 16:
        return []
    n = len(a)
    m = n - 5
    gems, gold, elix = a[0:m], a[1:m + 1], a[2:m + 2]
    dark, t1, t2 = a[3:m + 3], a[4:m + 4], a[5:m + 5]
    c = ((gems <= 100_000)
         & (gold >= 10_000) & (gold <= 200_000_000)
         & (elix >= 10_000) & (elix <= 200_000_000)
         & (dark <= 20_000_000)
         & (gold != elix) & (elix != dark) & (gold != dark)
         & (t1 == t2) & (t1 >= 100) & (t1 <= 2_000_000)
         & (t1 != elix) & (t1 != dark)
         & ((gold % 100 != 0) | (elix % 100 != 0)))
    return [(int(i) + 1) * 4 for i in np.nonzero(c)[0]]   # 指向 gold


def _verify(pid, addr):
    """读 6 个 word（gems,gold,elixir,dark,tail1,tail2）确认候选地址。"""
    nums = shell().read_words(addr - 4, 6)
    if not nums or len(nums) < 6:
        return None
    return dict(gems=nums[0], gold=nums[1], elixir=nums[2], dark=nums[3],
                tail1=nums[4], tail2=nums[5])


def _score(v, spec):
    """结构分（6）+ 每项对得上 +3 + elixir 完全相等 +2。满分 17。"""
    s = 0
    if 0 <= v["gems"] < 100_000:
        s += 2
    if 10_000 <= v["gold"] <= 200_000_000 and 10_000 <= v["elixir"] <= 200_000_000:
        s += 2
    if v["tail1"] == v["tail2"] and 100 <= v["tail1"] <= 2_000_000:
        s += 2
    for k, (ref, tol) in zip(("gold", "elixir", "dark"), spec):
        if ref and abs(v[k] - ref) <= max(2000, int(ref * tol)):
            s += 3
    if spec[1][0] and v["elixir"] == spec[1][0]:
        s += 2
    return s


def hud_seed():
    """画面 OCR 的读数。哪一项读不出来就留 None（那一项宁可用上一轮的值）。"""
    try:
        r = read_res_ocr_board()
    except Exception:
        return None
    if not r:
        return None
    out = tuple(r.get(k) if isinstance(r.get(k), int) and r.get(k) > 0 else None
                for k in ("gold", "elixir", "dark"))
    return out if any(out) else None


def read_res_ocr_board():
    img = grab()
    if img is None:
        return None
    return _ocr.read_res(img)


def _seed_sane(seed):
    """上一轮读数能不能当种子用。

    地址一旦读错，存下来的「上一轮读数」就是垃圾（实测见过 393222 / 458759 / 458758
    这种 elixir 和 dark 差 1 的值），拿垃圾当种子去精确搜索只会越搜越偏 ——
    所以先按常识过一遍：金币有下限、圣水和暗黑不可能几乎相等、数量级要像资源。
    """
    if not seed or len(seed) < 3:
        return False
    g, e, k = seed[0], seed[1], seed[2]
    for x in (g, e, k):
        if not isinstance(x, (int, float)) or x < 0:
            return False
    if g < 1_000:                        # 这个号的金币是七位数，小于 1000 只能是垃圾
        return False
    if e > 30_000_000 or k > 600_000:
        return False                 # 暗黑上限按 TH15 的 30 万给一倍余量
    if e and k and abs(e - k) <= max(2_000, int(e * 0.01)):
        return False                     # 圣水和暗黑几乎相等：典型垃圾结构
    return True


def _confirm(pid, addr, tries=2, gap=1.2):
    """定位成功后再读两遍交叉验证：值必须稳定（每秒自然增长几个，1% 窗口够宽）。

    两次差得离谱说明地址其实不对（比如落在别的结构上），那就别写进 state。
    """
    prev = None
    for i in range(tries):
        nums = shell().read_words(addr - 4, 4)
        if not nums or len(nums) < 4:
            return None
        v = dict(gems=nums[0], gold=nums[1], elixir=nums[2], dark=nums[3])
        if not sane(v):
            return None
        if prev is not None:
            for k in ("gold", "elixir", "dark"):
                if abs(v[k] - prev[k]) > max(2_000, int(prev[k] * 0.01)):
                    return None
        prev = v
        if i + 1 < tries:
            time.sleep(gap)
    return prev


def _seed_spec(hud, sample):
    """拼出搜索窗口：画面读到的用 0.5%，只有上一轮存下的用 2%，都没有就不约束。

    暗黑那项额外放宽：它只有五六位，OCR 会整位读错（实测真值 77,396 读成 177,396，
    差整整 100,000），而 gold/elixir 都是七位数、认得准。暗黑一旦被读错，exact3 和
    near 全部落空，整轮 300s 定位烧完还失败（2026-11-25 01:47 那轮就是这样挂的）。
    所以它跟上一轮差得离谱时干脆不拿来当锚，位置只靠 gold+elixir 定。
    """
    spec = []
    for i in range(3):
        ref, tol = (None, None)
        if hud and hud[i]:
            ref, tol = hud[i], 0.005
        elif sample and sample[i]:
            ref, tol = sample[i], 0.02
        if i == 2 and ref and sample and sample[i]:
            if abs(ref - sample[i]) > max(5000, int(sample[i] * 0.25)):
                ref, tol = None, None
        # 金币/圣水是七位数、平时认得准，但也会整位多读一个 1：实测 11-26 15:58 那轮，
        # 真值 elixir 6,396,886 被读成 16,396,886，而 _hud_agree 对 gold/elixir 是
        # 「有参考就必须全中」的死条件——一个错锚把所有正确候选全否决，重定位白烧 300s。
        # 仓库容量就那么点，一轮不可能涨过 50%（再加 2M 绝对兜底），超了就是 OCR 读错。
        if i in (0, 1) and ref and sample and sample[i]:
            if abs(ref - sample[i]) > max(2000000, int(sample[i] * 0.5)):
                ref, tol = None, None
        spec.append((ref, tol))
    return spec


def _hud_agree(v, spec, min_keys=2):
    """候选值必须和参考读数对得上：对得上的项 >= min_keys。

    gold / elixir 是七位数，OCR 认得准，超窗就一票否决；
    dark 只有五六位、会整位读错，超窗只算「没对上」，不否决——否则 g/e 两项全中
    也会被一个读错的暗黑判死，整轮定位空转 300s（2026-11-25 01:47 实录）。
    """
    hit, strong, strong_ref = 0, 0, 0
    for k, (ref, tol) in zip(("gold", "elixir", "dark"), spec):
        if not ref:
            continue
        if k in ("gold", "elixir"):
            strong_ref += 1
        if abs(v[k] - ref) > max(2000, int(ref * tol)):
            if k in ("gold", "elixir"):
                return False
            continue
        hit += 1
        if k in ("gold", "elixir"):
            strong += 1
    if strong_ref and strong < strong_ref:
        return False             # gold/elixir 有一个参考值就必须全中，不能拿暗黑凑数
    return hit >= min_keys


def _checks_of(spec_check):
    """spec_check 允许是「单个 spec」或「一组 spec」，统一成 list[spec]。

    单个 spec 形如 [(值, 窗口) x3]；一组 spec 形如 [spec, spec]。
    """
    if not spec_check:
        return [[]]
    first = spec_check[0]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
        return [list(c) for c in spec_check]
    return [list(spec_check)]


def _scan(pid, specs, spec_check, min_mb=4, deadline=None):
    """扫一遍内存，用所有搜索窗口同时匹配（省得每种模式各扫一遍，一次要一两分钟）。

    specs = [(mode, spec)]，列表顺序就是优先级。
    spec_check 是验收用的窗口，可以给一组（list of spec）：候选只要**对得上其中一组**
    就收。为什么要给多组：画面 OCR 偶尔会把某一项读错（实测真值 1,413,829 读成 62,811），
    拿单组窗口验收时所有正确候选都会被那个错值一票否决，整轮定位必然失败；
    再拿上一轮存下的读数做一组备用窗口，就能绕开一次误读。
    deadline（绝对时间戳）到了就停——这是防「一轮无限跑」的第一道闸。
    """
    checks = _checks_of(spec_check)
    segs = _segments(pid, min_mb)
    total = sum(x["size"] for x in segs)
    log(f"  重定位 扫 {len(segs)} 段 / {total / 1048576:.0f}MB "
        f"模式 {[m for m, _ in specs]}")
    t0 = time.time()
    if deadline is None:
        deadline = t0 + SCAN_BUDGET
    budget = max(1.0, deadline - t0)
    for seg in segs:
        off = 0
        while off < seg["size"]:
            left = deadline - time.time()
            if left <= 0:
                log(f"  到时限（{budget:.0f}s），停止这一级搜索（扫到 {seg['start']:#x}）")
                return None
            sz = min(BLK, seg["size"] - off)
            d = _pull_block(pid, seg["start"] + off, sz, timeout=left)
            base = seg["start"] + off
            off += sz
            if not d:
                continue
            cands = []
            for mode, spec in specs:
                if mode == "exact3":
                    vals = [v for v, _t in spec]
                    if any(v is None for v in vals):
                        continue
                    hits = _find_exact(d, vals)
                elif mode == "near":
                    hits = _find_near(d, spec)
                else:
                    hits = _find_shape(d)
                for o in hits[:6]:
                    cands.append((base + o, mode))
            for addr, mode in cands:
                v = _verify(pid, addr)
                if not v:
                    continue
                if not sane(v):
                    continue        # gems/dark 不像真资源：假地址，直接丢
                sc, ok, hit = 0, False, None
                for chk in checks:
                    s1 = _score(v, chk)
                    if s1 > sc:
                        sc, hit = s1, chk
                    if s1 >= 9 and _hud_agree(v, chk):
                        ok = True
                log(f"    候选 {addr:#x} via={mode} score={sc:.0f} "
                    f"g={v['gold']:,} e={v['elixir']:,} d={v['dark']:,}"
                    + ("" if ok else "（否）"))
                if ok:
                    v2 = _confirm(pid, addr)
                    if v2 is None:
                        log("      二次读数不稳，判为假地址，继续搜")
                        continue
                    return addr, mode, v2, time.time() - t0
            if time.time() > deadline:
                log(f"  到时限（{budget:.0f}s），停止这一级搜索")
                return None
    return None


def relocate(budget=None, stage=None):
    """地址失效后重新定位，返回 True/False。成功时写回 coc_state.json。

    搜索窗口按项拼（见 _seed_spec）：
      画面 OCR 读到的项 → 0.1% 窗口（读数只差几秒，资源值不会自己漂）
      只有上一轮存下的项 → 2% 窗口（中间可能被收取/升级改过）
      哪一项都没有 → 不约束那一项，靠另外两项 + 结构分把关
    一次扫描里同时用 exact3 和 near（省时间），逐类搜索（shape）当最后手段，
    而且必须有样本值参与打分 —— 否则结构特征会成片命中数据表。

    时间预算（这是防「一轮无限跑」的主要一处）：
      stage 单级搜索（精确 / 宽窗 / 结构兜底）的时限，默认 LOCATE_STAGE = 180s
      budget 整轮定位的总时限，默认 LOCATE_TOTAL = 300s，到点直接返回 False
    三级搜索一级比一级贵，每级都跑到底实测能烧十分钟以上；到点宁可这一轮不干活，
    也不能把游戏挂着白烧屏幕。
    """
    t0 = time.time()
    stage = float(stage or LOCATE_STAGE)
    total_budget = float(budget or LOCATE_TOTAL)
    hard_end = t0 + total_budget

    def mark(ok, timed_out=False, why=None, addr=None):
        global LOCATE_INFO
        LOCATE_INFO = dict(ok=bool(ok), sec=round(time.time() - t0, 1),
                           budget_s=total_budget, stage_s=stage,
                           timed_out=bool(timed_out), why=why,
                           addr=(f"{addr:#x}" if addr else None),
                           steps=list(_STEPS))
        return bool(ok)

    def dl():
        return min(time.time() + stage, hard_end)

    def out_of_time(what):
        log(f"  {what}：整轮定位到时限（{total_budget:.0f}s），放弃这一轮")

    _STEPS.clear()
    try:
        pid = find_pid()
    except RuntimeError as e:
        log(f"重定位失败：{e}")
        return mark(False, why=f"no_pid: {e}")
    s = shell()
    s.pid = pid

    st = load_state()
    sample = tuple(st["sample"]) if st.get("sample") else None
    if sample and not _seed_sane(sample):
        log(f"  上一轮读数 {sample} 像垃圾（不满足合理性校验），不用它当种子")
        sample = None
    hud = hud_seed()
    if not hud or not hud[0] or not hud[1]:      # 关键两项没读到，等画面稳一点再读
        for _ in range(2):
            time.sleep(2.0)
            hud = hud_seed()
            if hud and hud[0] and hud[1]:
                break
    spec = _seed_spec(hud, sample)
    # 验收窗口给两组：画面那组 + 上一轮读数那组。任一组对得上就收。
    # 只给一组的话，画面某一项认错一位就让所有正确候选都被一票否决（实测整轮定位必然失败）。
    checks = [spec]
    sample_spec = _seed_spec(None, sample) if sample else None
    if sample_spec and [(v, t) for v, t in sample_spec] != [(v, t) for v, t in spec]:
        checks.append(sample_spec)
    log(f"  窗口：画面 {hud} / 上一轮 {sample} → "
        f"{[(v, t) for v, t in spec]}"
        + ("（验收时与上一轮那组任一命中即收）" if len(checks) > 1 else ""))

    specs = []
    if hud and all(hud):
        specs.append(("exact3", spec))
    if any(v for v, _t in spec):
        specs.append(("near", spec))
    if sample and tuple(x or 0 for x in (hud or (None, None, None))) != sample:
        specs.append(("exact3", [(v, 0.02) for v in sample]))

    res = _scan(pid, specs, checks, min_mb=4, deadline=dl()) if specs else None
    _STEPS.append("exact/near>=4MB")
    if res is None and specs:
        if time.time() >= hard_end:
            out_of_time("第二级未开始")
            return mark(False, timed_out=True, why="stage1_exhausted")
        # 刚启动的游戏堆是新建的，资源结构常常落在小于 4MB 的小段里，
        # 第一遍（只扫 >=4MB）会漏；用同样的种子再扫一遍小段，比直接退到结构搜索稳。
        log("  第一遍没命中，扫小段再来一遍")
        res = _scan(pid, specs, checks, min_mb=1, deadline=dl())
        _STEPS.append("exact/near>=1MB")
    if res is None:
        shape_seed = [(v, 0.02) if v else (None, None) for v in (sample or hud or ())]
        if len(shape_seed) == 3:
            if time.time() >= hard_end:
                out_of_time("结构兜底未开始")
                return mark(False, timed_out=True, why="stage2_exhausted")
            log("  逐类搜索兜底（带样本值打分）")
            res = _scan(pid, [("shape", shape_seed)], checks, min_mb=1, deadline=dl())
            _STEPS.append("shape>=1MB")
        else:
            log("  没有种子也没有画面读数，拒绝用结构搜索写地址（容易命中数据表）")
            _STEPS.append("shape_skipped（没有种子）")
    if res is None:
        to = time.time() >= hard_end
        log(f"重定位失败，用时 {time.time() - t0:.0f}s（上限 {total_budget:.0f}s）")
        return mark(False, timed_out=to, why="no_candidate")

    addr, why, v, dt = res
    save_state({"gold": addr, "elixir": addr + 4, "dark": addr + 8, "gems_addr": addr - 4,
                "sample": [v["gold"], v["elixir"], v["dark"]],
                "pid": pid, "located_at": int(time.time()), "located_via": why})
    log(f"重定位成功 {addr:#x} via={why}（{dt:.0f}s） "
        f"g={v['gold']:,} e={v['elixir']:,} d={v['dark']:,}")
    return mark(True, why=why, addr=addr)


def invalidate(reason=""):
    """把地址标记为失效（保留 sample 当下一次的种子），下次读会自动重定位。"""
    st = load_state()
    if not st:
        return
    for k in ("gold", "elixir", "dark", "gems_addr"):
        st.pop(k, None)
    st["invalidated_at"] = int(time.time())
    st["invalidated_reason"] = reason
    save_state(st)
    log(f"地址标记失效（{reason}），下一轮重新定位")


def check_against_ocr(vals=None):
    """拿画面读数交叉核对内存读数。

    返回 [] 表示一致，返回 [(项, 内存值, 画面值)] 表示不一致，None 表示画面读不出来。
    """
    hud = hud_seed()
    vals = vals or read_res()
    if not hud or not vals:
        return None
    bad, checked = [], 0
    for k, r in zip(("gold", "elixir", "dark"), hud):
        if not r:
            continue                       # 画面那项没读出来，没法比
        checked += 1
        v = vals.get(k)
        if v is None or abs(v - r) > max(3000, int(r * 0.02)):
            bad.append((k, v, r))
    return bad if checked else None


def remember_sample(vals):
    """把最近一次可信读数存成下一轮重定位的种子（读到的值没变就不写盘）。"""
    if not isinstance(vals, dict):
        return
    st = load_state()
    if not st or not st.get("gold"):
        return
    if not all(isinstance(vals.get(k), int) for k in ("gold", "elixir", "dark")):
        return
    sample = [vals["gold"], vals["elixir"], vals["dark"]]
    pid = shell().pid or st.get("pid")
    if st.get("sample") == sample and st.get("pid") == pid:
        return
    st["sample"] = sample
    st["pid"] = pid
    save_state(st)


# ---------------------------------------------------------------- 镜头变换
def _kind(k):
    return {"coin": "gold"}.get(k, k)


def _by_kind(det):
    out = {}
    for k, x, y in det:
        out.setdefault(k, []).append((float(x), float(y)))
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def solve_transform(det, pairs, s_hint=None, tol=16.0):
    """从「检测到的气泡坐标」和「标定表里的同名点」反解缩放+平移。

    做法和 coc_calib 一样：先假设某个尺度 s，任取一个「检测点 ↔ 同类标定点」
    当锚点算出 (tx,ty)，再数这个变换下有多少气泡能对上（内点），取内点最多、
    残差最小的那组。不同尺度扫一遍就够，气泡是固定尺寸的 UI 元素。
    """
    det_by = _by_kind(det)
    cal_by = {k: np.array(v, float) for k, v in pairs.items() if v}
    if not det_by or not cal_by:
        return None
    scales = []
    if s_hint:
        lo, hi = max(0.60, s_hint - 0.10), min(2.60, s_hint + 0.10)
        scales += list(np.arange(lo, hi + 1e-9, 0.01))
    scales += list(np.arange(0.86, 1.20, 0.02))
    best = None
    for s in scales:
        # 候选平移：每个检测点配每个同类标定点（点数有限，够便宜）
        cands = []
        for k, D in det_by.items():
            C = cal_by.get(k)
            if C is None:
                continue
            for (dx, dy) in D[:8]:
                for (cx, cy) in C:
                    cands.append((dx - s * cx, dy - s * cy))
        for tx, ty in cands:
            n, err = 0, 0.0
            for k, D in det_by.items():
                C = cal_by.get(k)
                if C is None:
                    continue
                T = C * s + np.array([tx, ty])                      # (N,2)
                e = np.abs(T[None, :, :] - D[:, None, :]).max(axis=2).min(axis=1)
                hit = e <= tol
                n += int(hit.sum())
                err += float(e[hit].sum())
            key = (n, -(err / max(1, n)))
            if best is None or key > best[0]:
                best = (key, float(s), float(tx), float(ty))
    if not best or best[0][0] < 3:
        return None
    return dict(s=round(best[1], 4), tx=round(best[2], 2), ty=round(best[3], 2),
                matches=int(best[0][0]))


def refine(det, pairs, T, tol=16.0):
    """在匹配上的点对上做相似变换最小二乘，把 s 收敛到真值。"""
    cal_by = {k: np.array(v, float) for k, v in pairs.items() if v}
    P, Q = [], []
    for k, x, y in det:
        C = cal_by.get(k)
        if C is None:
            continue
        t = C * T["s"] + np.array([T["tx"], T["ty"]])
        e = np.abs(t - np.array([x, y], float)).max(axis=1)
        i = int(e.argmin())
        if e[i] <= tol:
            P.append(C[i])
            Q.append([x, y])
    if len(P) < 3:
        return None
    M, _ = cv2.estimateAffinePartial2D(np.float32(P).reshape(-1, 1, 2),
                                       np.float32(Q).reshape(-1, 1, 2), method=cv2.LMEDS)
    if M is None:
        return None
    # 第二轮起剔除残差大的点重拟合。为什么要这么做：气泡是按颜色分的（金/圣水/暗黑），
    # 而村庄里同时存在的「升级」「军队升级」「宝箱」这些气泡颜色也在同一色系，会被当成
    # 采集器气泡，再匹配到 pairs 里某个真实采集点上（它们不一定离得远），几颗脏点就能把
    # 相似变换拉偏几十到上百像素——表现就是升级阶段点什么都没命中，点到城墙/树上。
    Pv, Qv = np.float32(P), np.float32(Q)
    for _ in range(2):
        pred = cv2.transform(Pv.reshape(-1, 1, 2), M).reshape(-1, 2)
        r = np.abs(pred - Qv).max(axis=1)
        keep = r <= max(4.0, tol * 0.5)
        if keep.sum() < 3 or bool(keep.all()):
            break
        M2, _ = cv2.estimateAffinePartial2D(Pv[keep].reshape(-1, 1, 2),
                                           Qv[keep].reshape(-1, 1, 2), method=cv2.LMEDS)
        if M2 is None:
            break
        M = M2
        P, Q = [p for p, k in zip(P, keep) if k], [q for q, k in zip(Q, keep) if k]
        Pv, Qv = np.float32(P), np.float32(Q)
        if len(P) < 3:
            break
    s = float(np.hypot(M[0, 0], M[1, 0]))
    if not 0.6 <= s <= 2.6:
        return None
    return dict(s=round(s, 5), tx=round(float(M[0, 2]), 2), ty=round(float(M[1, 2]), 2),
                matches=len(P))


# ---------------------------------------------------------------- 锚点标定
def load_anchors():
    """读村庄锚点图（3 块静态建筑贴图 + 它们在标定视角下的中心）→ [(img, center)]"""
    z = np.load(ANCHORS)
    return [(z["a%d" % i], z["c%d" % i]) for i in range(len(z.files) // 2)]


def calibrate_anchors(img, s_hint=None):
    """用村庄里的静态建筑当锚点：一个锚点定（缩放+平移），其余锚点交叉验证。

    为什么需要它：气泡法要求「矿里有货、气泡在屏幕上」。矿空了、圣水满仓、
    或者弹层盖住采集器时，气泡就没了，整轮就没法换算坐标。锚点是常驻贴图，
    任何时刻都能匹配。
    """
    try:
        anchors = load_anchors()
    except Exception:
        return None
    if len(anchors) < 2:
        return None
    A, Ac = anchors[0]
    rest = anchors[1:]

    def scan(lo, hi, step):
        out = None
        s = lo
        while s <= hi + 1e-9:
            tp = cv2.resize(A, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            H, W = tp.shape[:2]
            r = cv2.matchTemplate(img, tp, cv2.TM_CCOEFF_NORMED)
            _, mx, _, ml = cv2.minMaxLoc(r)
            if mx >= 0.72:            # 0.80 太严：活动装饰（南瓜/彩灯）盖到锚点边角就再也匹配不上
                ax, ay = ml[0] + W / 2.0, ml[1] + H / 2.0
                tx, ty = ax - s * Ac[0], ay - s * Ac[1]
                tot, n = mx, 1
                for (B, Bc) in rest:
                    tpb = cv2.resize(B, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                    Hb, Wb = tpb.shape[:2]
                    px, py = s * Bc[0] + tx, s * Bc[1] + ty
                    R = 45
                    x0 = int(px - Wb / 2 - R)
                    y0 = int(py - Hb / 2 - R)
                    if (x0 < 0 or y0 < 0 or x0 + Wb + 2 * R > img.shape[1]
                            or y0 + Hb + 2 * R > img.shape[0]):
                        continue          # 这个锚点跑到画面外了，不是「全部锚点必须都在」
                    win = img[y0:y0 + Hb + 2 * R, x0:x0 + Wb + 2 * R]
                    rb = cv2.matchTemplate(win, tpb, cv2.TM_CCOEFF_NORMED)
                    mxb = float(rb.max())
                    if mxb < 0.55:        # 交叉验证也要有门槛，否则凑出来的定位看不出错
                        n = -1
                        break
                    tot += mxb
                    n += 1
                if n >= max(2, len(anchors) - 1):
                    sc = tot / n
                    if out is None or sc > out[0]:
                        out = (sc, s, tx, ty)
            s += step
        return out

    if s_hint:
        best = scan(max(0.80, s_hint - 0.07), min(1.30, s_hint + 0.07), 0.01)
        if not best or best[0] < 0.85:
            b2 = scan(0.84, 1.26, 0.03)
            if b2 and (not best or b2[0] > best[0]):
                best = b2
    else:
        best = scan(0.84, 1.26, 0.02)
    if best:
        b3 = scan(max(0.80, best[1] - 0.012), min(1.30, best[1] + 0.012), 0.003)
        if b3 and b3[0] >= best[0]:
            best = b3
    if not best:
        return None
    sc, ss, tx, ty = best
    return dict(s=round(ss, 5), tx=round(tx, 2), ty=round(ty, 2),
                matches=len(anchors), anchor_score=round(sc, 3))


def calibrate(img=None):
    """每轮都标一次镜头：锚点定缩放+平移，气泡残差再收敛一次；都不行退回缓存。

    标定视角（collectors_mem.json 的坐标）是「刚启动进村」那一屏；游戏重启后
    镜头会回到这个视角，所以 identity 兜底也常常是对的，错了会在点击后由
    内存读数暴露出来（点了没涨）。

    顺序：锚点（不依赖气泡存在）→ 气泡最小二乘细化 → 纯气泡法 → 缓存 → identity。
    """
    pairs = load_collectors()["pairs"]
    if img is None:
        img = grab()
    det = []
    if img is not None:
        try:
            det = [(_kind(k), int(x), int(y)) for (k, x, y, _sc) in _ocr.find_bubbles(img)]
        except Exception as e:
            log(f"  气泡检测失败 {e}")
    hint = (load_calib() or {}).get("s")
    T = None
    if img is not None:
        A = calibrate_anchors(img, s_hint=hint)
        if A:
            A["via"] = "anchors"
            if len(det) >= 4:
                R = refine(det, pairs, A, tol=14)
                if R and R["matches"] >= 4:
                    R["anchor_score"] = float(A.get("anchor_score") or 0)
                    R["via"] = "anchors+bubbles"
                    A = R
            T = A
    if T is None and det:
        T = solve_transform(det, pairs, s_hint=hint)
        if T:
            R = refine(det, pairs, T)
            if R and R["matches"] >= 3:
                R["via"] = "bubbles"
                T = R
            else:
                T["via"] = "bubbles-coarse"
            T["bubbles"] = len(det)
    if T is None:
        cached = load_calib()
        if cached and 0.6 <= cached.get("s", 0) <= 2.6:
            T = dict(cached)
            T["via"] = "cached"
        else:
            T = dict(s=1.0, tx=0.0, ty=0.0, matches=0, via="identity")
    T["at"] = int(time.time())
    # 可信度判定集中在这里出：调用方（研究）只看 T["trusted"]，不再自己各判各的。
    # 为什么多一档「近似可信」：一轮里升级完，游戏会把镜头推到刚开工那栋楼上，锚点贴图被
    # 进度条/气泡挡住大半，只剩 3 个能交叉验证 → 老规则直接判不可信，研究那一步就整步放弃
    # （实测连续 10 轮跳过，实验室空转了四天）。而这种推镜头的位移实测只有十来像素，
    # 只要和上次可信位姿差得少，拿它点实验室是安全的——点错了 open_lab_panel 还会用
    # 面板标题等级再验一次，不会一路错下去。
    trust = (T.get("via") in ("anchors", "anchors+bubbles", "bubbles")
             and int(T.get("matches") or 0) >= TRUST_MIN_MATCHES)
    if not trust and int(T.get("matches") or 0) >= 3 and str(T.get("via") or "").startswith("anchors"):
        lt = load_trusted()
        if (lt and 0.6 <= float(lt.get("s") or 0) <= 2.6
                and time.time() - int(lt.get("at") or 0) <= NEAR_MAX_AGE_S
                and abs(T["s"] - lt["s"]) <= NEAR_MAX_DS
                and abs(T["tx"] - lt["tx"]) <= NEAR_MAX_PX
                and abs(T["ty"] - lt["ty"]) <= NEAR_MAX_PX):
            T["via"] = "anchors+near"     # 近似可信：锚点命中少，但与上次可信位姿一致
            T["near_of"] = int(lt.get("at") or 0)
            trust = True
    T["trusted"] = bool(trust)
    if trust:
        save_trusted(dict(T))
    save_calib(T)
    log(f"镜头 {T['via']} s={T['s']:.4f} tx={T['tx']:.1f} ty={T['ty']:.1f} "
        f"匹配={T.get('matches', 0)} 气泡={T.get('bubbles', 0)}")
    return T


def apply_t(pts, T):
    s, tx, ty = T["s"], T["tx"], T["ty"]
    return [(int(round(s * x + tx)), int(round(s * y + ty))) for (x, y) in pts]


# 可以安全点击的「村庄画面」范围：右边这根 HUD（资源条 / 商店 / 宝石）和底部那条底栏
# （战斗 / 建造 / 商店）都盖在世界上，点进去会开商城、礼包弹层甚至「联机模式」页
# （实测：镜头被推到实验室那一屏时，4 个采集点落在 HUD 里，一轮下来一个资源没收着，
#   还顺手开了两次礼包弹层、一次战斗页）。
PLAY_AREA = (40, 120, 1780, 880)


def collect_points(T=None, cal=None):
    """17 个采集点换算到当前画面：可见的返回 (kind,x,y)，出屏/落在 HUD 上的返回 (kind,x,y,'offscreen')。"""
    cal = cal or load_collectors()
    T = T or dict(s=1.0, tx=0.0, ty=0.0)
    ax0, ay0, ax1, ay1 = PLAY_AREA
    out = []
    for k in ("gold", "elixir", "dark"):
        for (x, y) in cal["pairs"][k]:
            px, py = apply_t([(x, y)], T)[0]
            if ax0 <= px <= ax1 and ay0 <= py <= ay1:
                out.append((k, px, py))
            else:
                out.append((k, px, py, "offscreen"))
    dis = apply_t([cal["dismiss"]], T)[0]
    return out, dis


def find_bubbles(img=None):
    """内存后端里「要收的地方」= 标定表里的 17 个采集点，不是看图找气泡。

    返回形状和 coc_patrol2.find_bubbles 一致 (kind, x, y, score)，kind 用
    gold/elixir/dark（OCR 那版用 coin/elixir/dark），出屏的点不返回。
    """
    T = calibrate(img if img is not None else grab())
    pts, _dis = collect_points(T)
    out = [(k, x, y, 1.0) for (k, x, y) in [p[:3] for p in pts if len(p) == 3]]
    log(f"采集点 {len(out)}/{len(pts)} 可见（按标定表 + 镜头变换）")
    return out


# ---------------------------------------------------------------- 进村 / 弹层
def dismiss_promo(img=None, tries=3):
    """关掉盖住村庄的全屏弹层，返回关完之后的画面。

    判据要「压暗 + 命中推广文案」同时成立：只靠文案会在关掉之后再点一次
    （(1938,120) 在村庄里是圣水仓储图标，点开会弹出仓储信息），只靠亮度会被
    夜晚的村庄骗到。
    """
    for _ in range(max(1, tries)):
        if img is None:
            img = grab()
        if img is None:
            return None
        try:
            mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
            joined = " ".join(t["t"] for t in _ocr.ocr_all(img) if t.get("c", 0) >= 0.60)
        except Exception:
            return img
        hit = [w for w in PROMO_WORDS if w in joined]
        if not hit or mean >= 95:
            return img
        log(f"关掉全屏弹层（亮度 {mean:.0f}，命中 {'/'.join(hit)}）@{PROMO_CLOSE}")
        shell().tap(*PROMO_CLOSE, wait=1.5)
        img = None
    return grab()


def enter_village(max_steps=25, warmup=16):
    """进村 + 再把全屏弹层关掉。

    coc_patrol2 的 classify() 靠底栏「建造/战斗」认村庄，被弹层盖住时也照样
    判成村庄，所以这里补一步：认出推广页就点右上角 X。
    """
    img = _ocr.enter_village(max_steps=max_steps, warmup=warmup)
    if img is None:
        return None
    return dismiss_promo(img)


# ---------------------------------------------------------------- 收集
def collect(img=None, dry=False, cal=None, T=None):
    """走一遍 17 个采集点，逐点读内存判断有没有收到东西。

    返回 (before, after, ok, items)：items 是逐点的点击记录（含 offscreen 与 Δ），
    巡检入口直接写进 meta.json 给看板看。判据不是「画面变了」而是「这项资源涨了没有」，
    所以圣水满仓时不会误报成功，点空了也不会算成收到。

    T 可以传外面已经解好的镜头变换（巡检主循环刚算过，重复算一遍白花一秒多）。
    """
    t0 = time.time()
    if img is None:
        img = grab()
    before = read_res_vote(3)
    log(f"内存读数 收取前 {before}")
    w = read_workers(img)
    star = read_star(img)
    T = T or calibrate(img)
    pts, dis = collect_points(T, cal)
    log(f"采集点 {len([p for p in pts if len(p) == 3])}/{len(pts)} 可见 | dismiss {dis}")
    items, ok = [], 0
    if dry:
        log("dry：只算点位不点击")
        for item in pts:
            if len(item) == 4:
                items.append({"type": item[0], "x": item[1], "y": item[2],
                              "skip": "offscreen"})
            else:
                items.append({"type": item[0], "x": item[1], "y": item[2], "delta": None})
    else:
        cur = read_all() or {"gems": 0, "gold": before["gold"],
                             "elixir": before["elixir"], "dark": before["dark"]}
        for i, item in enumerate(pts):
            if len(item) == 4:
                items.append({"type": item[0], "x": item[1], "y": item[2],
                              "skip": "offscreen"})
                continue
            k, x, y = item
            # 采集前瞄一眼有没有挡屏弹层（礼包/商店）：有的话这一下就点在弹层上了，
            # 而且点了还会把弹层再叫出来一次（实测「专属礼包」在采集途中反复冒出来，
            # 一轮 12 个点全打空、资源一点没收到）
            try:
                import coc_mem_research as _r
                import coc_mem_panel as _p
                _im = grab()
                pp = _r.popup_x(_im) if _im is not None else None
                if pp:
                    log(f"  ⚠ 采集途中冒出来弹层 @{pp}，先关掉")
                    if os.environ.get("COC_DEBUG"):
                        cv2.imwrite("/tmp/collect_popup_%d.jpg" % int(time.time()), _im)
                    shell().tap(*pp, wait=1.2)
                elif _im is not None and _p.read(_im).get("open"):
                    # 上一个点没有气泡时点到建筑上了，面板还开着。面板底部的「升级」按钮
                    # 就在采集点附近，不关掉的话下一手可能直接点在升级上（绕过所有上限校验）
                    log("  （上一个点开出了面板，先取消选中）")
                    shell().tap(*dis, wait=0.35)
            except Exception:
                pass
            shell().tap(x, y, wait=0.40)
            nxt = read_all() or cur
            d = nxt[k] - cur[k]
            shell().tap(*dis, wait=0.22)      # 没气泡时点到建筑 → 点的这一下把面板关掉
            if d > 0:
                ok += 1
            items.append({"type": k, "x": x, "y": y, "delta": d})
            log(f"  [{i + 1}/{len(pts)}] {k} ({x},{y}) Δ={d}")
            cur = nxt
            if time.time() - t0 > 240:
                log("时间到，停止收集")
                break
    after = read_res_vote(3)
    if not dry:                 # dry 一次点击都不发（面板守卫也会点空地，这里跳过）
        clear_panels()
    gain = {k: (after[k] - before[k]) if (after.get(k) is not None
                                         and before.get(k) is not None) else None
            for k in ("gold", "elixir", "dark")}
    log(f"内存读数 收取后 {after} | 收到 {ok}/{len([p for p in pts if len(p) == 3])} | 增量 {gain}")
    visible = len([p for p in pts if len(p) == 3])
    if not dry and ok == 0 and visible >= 5 and not any((v or 0) > 0 for v in gain.values()):
        # 点了十几个点一个没涨：要么矿真空，要么地址不准。用画面读数核一下。
        bad = check_against_ocr(after)
        if bad:
            log(f"⚠ 内存读数和画面读数不一致 {bad}，判定地址失效")
            invalidate("round_zero_gain_ocr_mismatch")
        elif bad == []:
            log("⚠ 一个点都没收到（矿可能是空的）；读数与画面一致，地址没问题")
        else:
            log("⚠ 一个点都没收到；画面读数也用不上，没法核对地址")
    remember_sample(after)
    return before, after, ok, items


# ---------------------------------------------------------------- 升级
def upgrade_from_list(max_price=None, kinds=("gold", "elixir"), dry=False,
                      items=None, res=None, w0=None):
    """挑一条付得起的升掉。签名和 OCR 那版一致，区别在「预算」和「有没有成功」都看内存。

    - 预算 = 内存读到的当前值（逐位精确），不用 OCR 投票；
    - dry=True 只挑不点，用来验证升级路径（会花钱，实测一律 dry）；
    - 真点的部分复用 coc_patrol2 的列表点击流程（面板守卫只有一份实现），
      点完再读内存核对扣款：扣到个位才算成功。
    """
    res0 = res if (res and res.get("gold")) else read_res_vote(3)
    if not res0 or res0.get("gold") is None:
        log("  读不到内存资源，放弃升级")
        return None
    w_start = w0 or read_workers(grab())
    if items is None:
        im = open_build_list()
        items = read_build_list(im) if im is not None else []
    if not items:
        log("  读不到建筑列表")
        clear_panels()
        return None

    budget = {"gold": res0.get("gold") or 0, "elixir": res0.get("elixir") or 0,
              "dark": res0.get("dark") or 0}
    pool = [i for i in items if i.get("kind") in kinds]
    pool.sort(key=lambda i: i["price"])
    log(f"  内存预算 g={budget['gold']:,} e={budget['elixir']:,} d={budget['dark']:,} | "
        f"候选 {[(i['name'], i['price'], i['kind']) for i in pool[:3]]}")
    picked = None
    for it in pool:
        if max_price and it["price"] > max_price:
            continue
        if it["price"] > budget.get(it["kind"], 0) * 0.9:
            continue
        picked = it
        break
    if picked is None:
        log("  没有付得起的项目")
        clear_panels()
        return None
    if dry:
        log(f"  dry：选中 {picked['name']} {picked['price']:,}（{picked['kind']}），不点击")
        clear_panels()
        return picked, w_start, w_start

    before = read_res_vote(2)
    got = _ocr.upgrade_from_list(max_price=max_price, kinds=kinds, dry=False,
                                 items=[picked], res=res0, w0=w_start)
    after = read_res_until(3, tries=4, gap=1.2)
    cur = after.get(picked["kind"]), before.get(picked["kind"])
    spent = (cur[1] - cur[0]) if (cur[0] is not None and cur[1] is not None) else None
    if spent is not None and spent >= picked["price"] * 0.9:
        log(f"  内存确认扣费 {picked['kind']} {spent:,}（报价 {picked['price']:,}）")
        w1 = read_workers(grab())
        remember_sample(after)
        return picked, w_start, w1
    log(f"  内存没看到扣费（spent={spent}，OCR 结果={bool(got)}），按失败处理")
    clear_panels()
    return None


# ---------------------------------------------------------------- CLI
def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "read"
    t0 = time.time()
    try:
        if cmd == "read":
            print(json.dumps(read_all(), ensure_ascii=False))
        elif cmd == "locate":
            ok = relocate()
            print(json.dumps({"ok": ok, "info": LOCATE_INFO, "state": load_state()},
                             ensure_ascii=False))
            return 0 if ok else 1
        elif cmd == "cold":
            # 一轮开始：唤醒解锁 → 没跑就拉起 → 等进村
            print(json.dumps(start_round(), ensure_ascii=False))
        elif cmd == "close":
            print(json.dumps(close_game(), ensure_ascii=False))
        elif cmd == "sleep":
            print(json.dumps(sleep_screen(), ensure_ascii=False))
        elif cmd == "power":
            print(json.dumps(power_state(), ensure_ascii=False))
        elif cmd == "village":
            k, j = village_probe()
            print(json.dumps({"kind": k, "text": j[:120]}, ensure_ascii=False))
        elif cmd == "shutdown":
            print(json.dumps(shutdown(), ensure_ascii=False))
        elif cmd == "calib":
            img = grab()
            T = calibrate(img)
            pts, dis = collect_points(T)
            print(json.dumps({"T": T, "dismiss": dis,
                              "visible": len([p for p in pts if len(p) == 3]),
                              "total": len(pts), "pts": [p[:3] for p in pts if len(p) == 3]},
                             ensure_ascii=False))
        elif cmd == "promo":
            init_capture()
            dismiss_promo()
            print(json.dumps({"ok": True}, ensure_ascii=False))
        elif cmd == "collect":
            init_capture()
            dismiss_promo()
            before, after, ok, items = collect(dry="--dry" in args)
            print(json.dumps({"before": before, "after": after, "ok": ok,
                              "items": items, "dur_s": round(time.time() - t0, 1)},
                             ensure_ascii=False))
        elif cmd == "upgrade":
            print(json.dumps(upgrade_from_list(max_price=3_000_000,
                                               dry="--dry" in args or True),
                             ensure_ascii=False, default=str))
        else:
            print(__doc__)
            return 2
    finally:
        print(f"[{time.time() - t0:.1f}s]", file=sys.stderr)
        close_capture()
        close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
