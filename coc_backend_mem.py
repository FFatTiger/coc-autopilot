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
  python3 coc_backend_mem.py locate              # 强制重定位
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

# coc_patrol2.grab() 里用到的模块级计数器 _mc_fail 只在一帧成功取回之后才会被赋值，
# minicap 起不来时第一次 grab 会抛 NameError。这里先把它补齐：内存后端不能因为
# 抓帧层的小毛病整轮崩掉（minicap 不行就退回 screencap），coc_patrol2 本身不动。
if not hasattr(_ocr, "_mc_fail"):
    _ocr._mc_fail = 0

STATE = os.path.join(HERE, "coc_state.json")          # 资源地址 + 最近一次读数（定位种子）
COLLECTORS = os.path.join(HERE, "collectors_mem.json")  # 17 个采集点 + dismiss（标定视角）
CALIB = os.path.join(HERE, "mem_calib.json")          # 当前镜头变换缓存
ANCHORS = os.path.join(HERE, "coc_templates", "village_anchors.npz")  # 村庄锚点图（定缩放）

MARK = "__COC_MEM__"
PAGE = 4096
BLK = 8 << 20            # 重定位时每块读 8MB
DD_TIMEOUT = 8           # 手机端 timeout：读到未映射页会永久阻塞，必须让远端自己超时
SCAN_BUDGET = 300        # 单个搜索模式的墙钟预算（秒）

# 重定位只扫这几类段：实测资源结构在 libc_malloc 里；其余段命中率低还费时间
SKIP_NAME = ("dalvik", "jit", "ashmem", "dev/", ".so", ".oat", ".vdex", ".art",
             "linker", "gralloc", "kgsl", "stack", "vectors", ".dex", ".apk")


# ---------------------------------------------------------------- adb 原语
def _adb(args, timeout=30, **kw):
    cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + list(args)
    kw.setdefault("capture_output", True)
    return subprocess.run(cmd, timeout=timeout, **kw)


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
def awake():
    """屏幕是不是亮着（KEYCODE_SLEEP 之后 dumpsys 里是 Asleep/Dozing）。"""
    return "Awake" in shell().run("dumpsys power | grep -m1 mWakefulness=")


def focus():
    """当前前台窗口。

    注意：dumpsys window 里 mCurrentFocus 会先打印一行 null 再打印真实值，
    用 -m1 会永远拿到 null，导致每轮都误判「游戏不在前台」而去拉起 App（会白等 40s）。
    """
    return shell().run("dumpsys window | grep mCurrentFocus= | tail -2")


def ensure_ready():
    """确保 屏幕亮 + 游戏在前台 + pid 有效。返回 (changed, [原因])。

    和 OCR 后端「每轮 force-stop 重开游戏」不同：内存后端要靠游戏进程长期活着，
    地址才能一直有效（重开一次就要重新定位一两分钟）。所以这里只补缺失的那一项。
    """
    s = shell()
    reasons = []
    if not awake():
        s.run("input keyevent KEYCODE_WAKEUP")
        time.sleep(0.9)
        reasons.append("wakeup")
    if PKG not in focus():
        s.run(f"am start -n {PKG}/{ACT}")
        time.sleep(3.5)
        reasons.append("relaunch")
    if not s.alive():
        try:
            s.pid = find_pid()
        except RuntimeError as e:
            raise RuntimeError(f"游戏进程不在，也拉不起来：{e}")
        reasons.append("pid=%d" % s.pid)
    return bool(reasons), reasons


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


def load_collectors():
    with open(COLLECTORS) as f:
        return json.load(f)


# ---------------------------------------------------------------- 读数
def sane(a):
    if not a:
        return False
    if not all(0 <= a[k] <= 200_000_000 for k in ("gems", "gold", "elixir", "dark")):
        return False
    if a["gold"] == 0 and a["elixir"] == 0:
        return False
    if a["gems"] > 5_000_000:
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


def _pull_block(pid, addr, size):
    """读一段内存到本地。<关键> 必须带手机端 timeout，否则未映射页会把 adb 通道堵死。"""
    size = (size // PAGE) * PAGE or PAGE
    skip, cnt = addr // PAGE, size // PAGE
    remote = f"/data/local/tmp/_cocscan.bin"
    cmd = (f"timeout {DD_TIMEOUT} dd if=/proc/{pid}/mem of={remote} bs={PAGE} "
           f"skip={skip} count={cnt} 2>/dev/null; chmod 666 {remote}")
    try:
        _adb(["shell", f"su -c {cmd!r}"], timeout=25)
    except subprocess.TimeoutExpired:
        return b""
    try:
        r = _adb(["pull", remote, "/tmp/_cocscan.bin"], timeout=60)
    except subprocess.TimeoutExpired:
        return b""
    if r.returncode != 0:
        return b""
    try:
        with open("/tmp/_cocscan.bin", "rb") as f:
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
    if e > 200_000_000 or k > 20_000_000:
        return False
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
    """拼出搜索窗口：画面读到的用 0.1%，只有上一轮存下的用 2%，都没有就不约束。"""
    spec = []
    for i in range(3):
        if hud and hud[i]:
            spec.append((hud[i], 0.005))
        elif sample and sample[i]:
            spec.append((sample[i], 0.02))
        else:
            spec.append((None, None))
    return spec


def _hud_agree(v, spec, min_keys=2):
    """候选值必须和参考读数对得上：对得上的项 >= min_keys，且没有一项超窗。"""
    hit = 0
    for k, (ref, tol) in zip(("gold", "elixir", "dark"), spec):
        if not ref:
            continue
        if abs(v[k] - ref) > max(2000, int(ref * tol)):
            return False
        hit += 1
    return hit >= min_keys


def _scan(pid, specs, spec_check, min_mb=4):
    """扫一遍内存，用所有搜索窗口同时匹配（省得每种模式各扫一遍，一次要一两分钟）。

    specs = [(mode, spec)]，列表顺序就是优先级。
    spec_check 是最终验收用的窗口（含结构搜索的样本值）。
    """
    segs = _segments(pid, min_mb)
    total = sum(x["size"] for x in segs)
    log(f"  重定位 扫 {len(segs)} 段 / {total / 1048576:.0f}MB "
        f"模式 {[m for m, _ in specs]}")
    t0 = time.time()
    for seg in segs:
        off = 0
        while off < seg["size"]:
            sz = min(BLK, seg["size"] - off)
            d = _pull_block(pid, seg["start"] + off, sz)
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
                sc = _score(v, spec_check)
                ok = sc >= 9 and _hud_agree(v, spec_check)
                log(f"    候选 {addr:#x} via={mode} score={sc:.0f} "
                    f"g={v['gold']:,} e={v['elixir']:,} d={v['dark']:,}"
                    + ("" if ok else "（否）"))
                if ok:
                    v2 = _confirm(pid, addr)
                    if v2 is None:
                        log("      二次读数不稳，判为假地址，继续搜")
                        continue
                    return addr, mode, v2, time.time() - t0
            if time.time() - t0 > SCAN_BUDGET:
                return None
    return None


def relocate():
    """地址失效后重新定位，返回 True/False。成功时写回 coc_state.json。

    搜索窗口按项拼（见 _seed_spec）：
      画面 OCR 读到的项 → 0.1% 窗口（读数只差几秒，资源值不会自己漂）
      只有上一轮存下的项 → 2% 窗口（中间可能被收取/升级改过）
      哪一项都没有 → 不约束那一项，靠另外两项 + 结构分把关
    一次扫描里同时用 exact3 和 near（省时间），逐类搜索（shape）当最后手段，
    而且必须有样本值参与打分 —— 否则结构特征会成片命中数据表。
    """
    t0 = time.time()
    try:
        pid = find_pid()
    except RuntimeError as e:
        log(f"重定位失败：{e}")
        return False
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
    log(f"  窗口：画面 {hud} / 上一轮 {sample} → "
        f"{[(v, t) for v, t in spec]}")

    specs = []
    if hud and all(hud):
        specs.append(("exact3", spec))
    if any(v for v, _t in spec):
        specs.append(("near", spec))
    if sample and tuple(x or 0 for x in (hud or (None, None, None))) != sample:
        specs.append(("exact3", [(v, 0.02) for v in sample]))

    res = _scan(pid, specs, spec, min_mb=4) if specs else None
    if res is None and specs:
        # 刚启动的游戏堆是新建的，资源结构常常落在小于 4MB 的小段里，
        # 第一遍（只扫 >=4MB）会漏；用同样的种子再扫一遍小段，比直接退到结构搜索稳。
        log("  第一遍没命中，扫小段再来一遍")
        res = _scan(pid, specs, spec, min_mb=1)
    if res is None:
        shape_seed = [(v, 0.02) if v else (None, None) for v in (sample or hud or ())]
        if len(shape_seed) == 3:
            log("  逐类搜索兜底（带样本值打分）")
            res = _scan(pid, [("shape", shape_seed)], shape_seed, min_mb=1)
        else:
            log("  没有种子也没有画面读数，拒绝用结构搜索写地址（容易命中数据表）")
    if res is None:
        log(f"重定位失败，用时 {time.time() - t0:.0f}s")
        return False

    addr, why, v, dt = res
    save_state({"gold": addr, "elixir": addr + 4, "dark": addr + 8, "gems_addr": addr - 4,
                "sample": [v["gold"], v["elixir"], v["dark"]],
                "pid": pid, "located_at": int(time.time()), "located_via": why})
    log(f"重定位成功 {addr:#x} via={why}（{dt:.0f}s） "
        f"g={v['gold']:,} e={v['elixir']:,} d={v['dark']:,}")
    return True


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
            if mx >= 0.80:
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
                        break
                    win = img[y0:y0 + Hb + 2 * R, x0:x0 + Wb + 2 * R]
                    rb = cv2.matchTemplate(win, tpb, cv2.TM_CCOEFF_NORMED)
                    tot += float(rb.max())
                    n += 1
                if n == len(anchors):
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
            print(json.dumps({"ok": ok, "state": load_state()}, ensure_ascii=False))
            return 0 if ok else 1
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
