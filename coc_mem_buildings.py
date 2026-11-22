#!/usr/bin/env python3
"""从进程内存直接枚举村庄建筑：类型名 / 等级 / 网格坐标 / 屏幕坐标 / 可升级目标。

这是内存后端「知道自己能升什么」的那一层：资源够不够是一回事，能升什么、升了会不会
点错建筑是另一回事。全部信息都从内存读，不依赖画面识别。

内存结构（2026-11-21 逆向，19.100.1，实测验证，详见 docs/FINDINGS_BUILDINGS.md）

  「建筑池」= 一段连续的 component 数组，stride 0x250，段首到段尾 vtable 相同。
  实测 549 个对象 = 392 个家乡建筑 + 157 个夜世界(BB)建筑（两座村庄同一个池）。

  建筑对象（stride 0x250）
    +0x00  vtable              （libg .data.rel.ro 内；本版 = libg_base + 0x214BBD8）
    +0x08  ptr                 （池内共享）
    +0x10  ptr                 （本对象的子 component，其 +0x08 指回本对象）
    +0x18  ptr  → LogicData    （类型！固定不变，同类型的实例共用）
    +0x20  u16 x 固定点 8.8    （= 网格坐标 x*2，单位半个 tile，值/256 得到 3..44）
    +0x24  u16 y 固定点 8.8    （同上；y 轴与 x 轴在等距投影下 45 度错开）
    +0x2c  u8  村庄            （0=家乡 1=夜世界；与名字前缀 BB 完全一致）
    +0x30  u32 对象序号        （0x1dcd6500 + idx，两座村庄各自从 0 计）
    +0x160 u32 等级（0 基）    （显示等级 = 这个值 + 1；Barracks 13 ↔ 面板 14 级，
                                Town Hall 11 ↔ TH12，暗黑重油钻井 3 ↔ 面板 4 级）
    +0x114 u32 每实例随机数

  LogicData（类型对象）
    +0x08  u32 名字长度（两次）
    +0x10  char[24] 内联名字（长度>23 时这里是指针）
    +0x18  u32 tid             （1000000 = Army Camp 起，按逻辑数据表顺序递增）
    +0x20  u32 a/u32 a
    +0x30  u32 全局最高等级 b/u32 b  （Wall=13, Gold Mine=18, Cannon=21 …）

坐标 → 屏幕（标定视图，s=1.0 那一屏，运行时再乘 coc_backend_mem.calibrate 的 s/tx/ty）

    屏幕 = M · (x, y) + t
    M = [[ 9.2171, -9.2126],          # 等距投影：a=9.217, b=6.917, b/a=0.7505
         [ 6.9170,  6.9092]]
    t ≈ (1115.2, -198.5)              # 17 个采集器气泡联合最小二乘，最大残差 2.7px
  （x,y 是对象里的原始值，不做 /2；M 已经是相对原始值的比例）

用法:
  python3 coc_mem_buildings.py --inv            # 打印全部建筑清单 + 分类型统计
  python3 coc_mem_buildings.py --inv --json     # 同上，JSON
  python3 coc_mem_buildings.py --targets        # 可升级目标（按价格/等级排序）
  python3 coc_mem_buildings.py --locate         # 只做定位，打印池地址
  python3 coc_mem_buildings.py --probe GOLD     # 打印名字里含 GOLD 的建筑
"""
import argparse
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import coc_backend_mem as mem

STATE = os.path.join(HERE, "building_pool.json")        # 池地址缓存
PRICES = os.path.join(HERE, "building_prices.json")     # (类型,等级) → 价格
TYPES = os.path.join(HERE, "building_types.json")       # 类型元数据缓存（名字/tid/等级数）
BLOCKED = os.path.join(HERE, "building_blocked.json")   # (类型,等级) → 被游戏挡住（TH 上限等）
OFFSETS = os.path.join(HERE, "tap_offsets.json")        # 类型 → 点击偏移（面板探测学到的）

# 投影点校准在资源气泡上，气泡浮在建筑顶部 → 投影点落在建筑顶棱，本体在下方 30-45px。
# 所以默认往下 28px；tap_offsets.json 里有实测值的按类型覆盖。
DY_DEFAULT = 28
DY_PRIOR = {"Eagle Artillery": 38, "Army Camp": 45, "Gold Storage": 30,
            "Elixir Storage": 30, "Wall": 10}

# ---- 本版 libg（19.100.1）常量，偏移量在同一个 .so 内稳定；换版本要重新量 ----
VT_OFF_BUILDING = 0x214BBD8      # 建筑池 vtable 在 libg 内的偏移（实测 0x6f1c08bbd8 - base）
DATA_REL_RO = (0x205A480, 0x134B08)   # .data.rel.ro 在 libg 内的 (偏移, 长度)
STRIDE = 0x250
MIN_RUN = 120                    # 认为「这是一个池」的最少连续对象数
# 建筑类 LogicData 的 tid 区间（1000000 = Army Camp 起，按逻辑数据表顺序递增）。
# 不在这个区间的（例如 K2* 战斗场景对象 0xcef2xxx）不是村庄建筑，要排除。
TID_LO, TID_HI = 0xF4240, 0xF4400

OFF_VT, OFF_TYPE, OFF_X, OFF_Y = 0x00, 0x18, 0x20, 0x24
OFF_VILLAGE, OFF_IDX, OFF_LEVEL = 0x2C, 0x30, 0x160

# 坐标 → 标定视图屏幕
PROJ_M = ((9.2171, -9.2126), (6.9170, 6.9092))
PROJ_T = (1115.19, -198.48)      # gold 组的解（与 elixir/dark 相差 ≤20px，都是气泡中心）

BB_PREFIX = "BB "                # 夜世界（Builder Base）


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 点击偏移
def load_offsets():
    """tap_offsets.json → {类型: (dx, dy)}

    三种写法都认：
      "Cannon": 28                     → (0, 28)     只用竖向偏移（历史格式）
      "Cannon": [0, 28]               → (0, 28)     中间过渡格式（脚本整体写出过列表）
      "Mortar": {"dx": 40, "dy": 55}  → (40, 55)    投影点偏了、需要横向补偿时用
    """
    try:
        d = json.load(open(OFFSETS))
    except Exception:
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = (float(v.get("dx", 0) or 0), float(v.get("dy", 0) or 0))
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            out[k] = (float(v[0] or 0), float(v[1] or 0))
        else:
            out[k] = (0.0, float(v))
    return out


def tap_off_of(name, offsets=None, default=None):
    """这个类型的点击偏移 (dx, dy)：实测表 > 先验 > 默认"""
    o = load_offsets() if offsets is None else offsets
    if name in o:
        return o[name]
    return (0.0, DY_PRIOR.get(name, DY_DEFAULT if default is None else default))


def tap_dy_of(name, offsets=None, default=None):
    """兼容旧调用：只要竖向偏移"""
    return tap_off_of(name, offsets, default)[1]


# ---------------------------------------------------------------- ELF / 段
def elf_sections(path):
    """只解析 section header，返回 {name: (addr, size, offset)}；解析失败返回 {}"""
    try:
        with open(path, "rb") as fh:
            d = fh.read(64 + 4096)
            if d[:4] != b"\x7fELF":
                return {}
            is64 = d[4] == 2
            if not is64:
                return {}
            (e_shoff,) = struct.unpack_from("<Q", d, 0x28)
            e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3A)
            fh.seek(e_shoff)
            sh = fh.read(e_shentsize * e_shnum)
            hdrs = [struct.unpack_from("<IIQQQQIIQQ", sh, i * e_shentsize)
                    for i in range(e_shnum)]
            stroff, strsize = hdrs[e_shstrndx][4], hdrs[e_shstrndx][5]
            fh.seek(stroff)
            strtab = fh.read(strsize)
            out = {}
            for h in hdrs:
                nm = strtab[h[0]:strtab.find(b"\0", h[0])].decode("utf8", "replace")
                out[nm] = (h[3], h[5], h[4])       # addr, size, offset
            return out
    except Exception:
        return {}


# ---------------------------------------------------------------- 主类
class Buildings:
    """建筑池的定位 + 读取 + 类型解析 + 价格表。一个进程一份，用完 close()。"""

    def __init__(self, s=None, log=None):
        self.s = s or mem.shell()
        self.pid = self.s.pid
        self.log = log or (lambda *a: None)
        self._maps = None
        self._libg = None
        self._types = {}
        self._pool = None
        self._price_cache = self._load_prices()

    # ---------------- 基础 ----------------
    def maps(self, fresh=False):
        if self._maps is None or fresh:
            self._maps = mem.maps_raw(self.pid)
        return self._maps

    def libg_base(self):
        if self._libg is None:
            for r in self.maps():
                if r["name"].endswith("libg.so") and r["off"] == 0:
                    self._libg = r["start"]
                    break
        return self._libg

    def vt_range(self):
        b = self.libg_base()
        off, sz = DATA_REL_RO
        return b + off, b + off + sz

    def vt_building(self):
        return self.libg_base() + VT_OFF_BUILDING

    def read(self, va, size):
        """读一段内存（走常驻 root shell，base64 传回）。失败返回 b""

        dd 的 skip 以页为单位，所以先对齐到页首再切掉页内偏移量。
        """
        return self.s.read_bytes(va, size)

    def u32(self, va):
        b = self.read(va, 4)
        return struct.unpack_from("<I", b, 0)[0] if len(b) >= 4 else None

    def u64(self, va):
        b = self.read(va, 8)
        return struct.unpack_from("<Q", b, 0)[0] if len(b) >= 8 else None

    # ---------------- 定位建筑池 ----------------
    def _state(self):
        try:
            return json.load(open(STATE))
        except Exception:
            return {}

    def _save_state(self, d):
        d = dict(d)
        d["libg"] = self.libg_base()
        d["pid"] = self.pid
        d["at"] = now()
        try:
            json.dump(d, open(STATE, "w"), indent=1)
        except Exception:
            pass

    def _probe_run(self, va):
        """从 va 开始按 stride 数连续同 vtable 的对象个数（每段 24 个对象探测）"""
        vt = self.vt_building()
        n = 0
        step = 24
        while True:
            b = self.read(va + n * STRIDE, STRIDE * step)
            if not b:
                break
            got = 0
            for i in range(min(step, len(b) // STRIDE)):
                (v,) = struct.unpack_from("<Q", b, i * STRIDE)
                if v != vt:
                    return n + got
                got += 1
            n += got
            if got < step:
                return n
            if n > 5000:
                return n

    def _run_here(self, va):
        """判断 va 是不是池首：本对象 vtable 命中，且前一个 stride 处不是同 vtable"""
        vt = self.vt_building()
        if self.u64(va) != vt:
            return False
        return self.u64(va - STRIDE) != vt

    def _scan_region_file(self, path, va0, vt):
        """本地 mmap 扫描一段：找 stride 连续且 vtable 相同的 run，返回 [(base, n)]

        只映射不读入内存（mmap + numpy 视图），不会把 GB 级数据塞进 RAM。
        """
        import mmap as _mm
        import numpy as _np
        out = []
        with open(path, "rb") as fh:
            sz = os.path.getsize(path)
            if sz < STRIDE * MIN_RUN:
                return out
            mm = _mm.mmap(fh.fileno(), 0, access=_mm.ACCESS_READ)
            words = _np.frombuffer(mm, dtype="<u8", count=sz // 8)
            hits = _np.where(words == vt)[0]
            del words
            if len(hits) < MIN_RUN:
                mm.close()
                return out
            pos = hits.astype(_np.int64) * 8
            del hits
            # 找 stride 连续段（pos 已升序）
            d = _np.diff(pos)
            brk = _np.where(d != STRIDE)[0]
            starts = _np.concatenate([[0], brk + 1])
            ends = _np.concatenate([brk + 1, [len(pos)]])
            lens = ends - starts
            for k in _np.argsort(-lens)[:4]:
                if lens[k] < MIN_RUN:
                    break
                out.append((va0 + int(pos[starts[k]]), int(lens[k])))
            del pos, d, brk, starts, ends, lens
            mm.close()
        return out

    def _pull_region(self, r, local="/tmp/_cocbldg.bin"):
        cmd = (f"dd if=/proc/{self.pid}/mem bs=4096 skip={r['start'] // 4096} "
               f"count={r['size'] // 4096} of=/data/local/tmp/_bldg.bin 2>/dev/null; "
               f"chmod 666 /data/local/tmp/_bldg.bin")
        self.s.run(cmd, timeout=180)
        try:
            p = mem._adb(["pull", "/data/local/tmp/_bldg.bin", local], timeout=300)
        except Exception:
            return None
        self.s.run("rm -f /data/local/tmp/_bldg.bin")
        if p.returncode != 0 or not os.path.exists(local):
            return None
        return local

    def locate(self, force=False):
        """返回 dict(base, count, vt, region)；找不到返回 None

        三级策略：缓存地址 → 缓存地址邻域 → 拉候选段到本地扫。
        游戏重载/切后台后堆会动，第一级失效、第二级常常还能命中。
        """
        if self._pool and not force:
            return self._pool
        st = self._state()
        vt = self.vt_building()
        if not vt or vt < 0x1000:
            self.log("[池] 拿不到 libg 基址")
            return None
        # tier0: 缓存地址
        if not force and st.get("base") and st.get("libg") == self.libg_base():
            base = int(st["base"])
            if self._run_here(base):
                n = self._probe_run(base)
                if n >= MIN_RUN:
                    self._pool = dict(base=base, count=n, vt=vt, via="cached",
                                      region=st.get("region", ""))
                    self.log("[池] 缓存命中 %#x, %d 个" % (base, n))
                    return self._pool
        # tier1: 缓存地址邻域
        if not force and st.get("base") and st.get("libg") == self.libg_base():
            base = int(st["base"])
            for d in (STRIDE, -STRIDE, 0x1000, -0x1000, 0x10000, -0x10000,
                      0x100000, -0x100000, 0x400000, -0x400000):
                cand = base + d
                if cand < 0x1000:
                    continue
                if self._run_here(cand):
                    n = self._probe_run(cand)
                    if n >= MIN_RUN:
                        self._pool = dict(base=cand, count=n, vt=vt, via="near",
                                          region=st.get("region", ""))
                        self._save_state(dict(base=cand, count=n,
                                              region=st.get("region", "")))
                        self.log("[池] 邻域命中 %#x, %d 个" % (cand, n))
                        return self._pool
        # tier2: 拉候选段到本地扫（toybox grep 在二进制长行上会漏匹配，不能用在设备上过滤）
        cands = [r for r in self.maps(fresh=True)
                 if r["perm"] == "rw-p" and r["size"] >= (1 << 20)
                 and (r["name"] == "" or "libc_malloc" in r["name"])]
        cands.sort(key=lambda r: -r["size"])
        # 上次命中的段大小先试
        last = st.get("region_size")
        if last:
            cands.sort(key=lambda r: (r["size"] != last, -r["size"]))
        self.log("[池] 全扫 %d 段, %.0fMB" % (len(cands),
                                          sum(r["size"] for r in cands) / 1048576))
        t0 = time.time()
        for i, r in enumerate(cands):
            path = self._pull_region(r)
            if not path:
                continue
            found = self._scan_region_file(path, r["start"], vt)
            try:
                os.unlink(path)
            except OSError:
                pass
            for base, ln in found:
                if not self._run_here(base):
                    for back in range(1, 6):
                        if self._run_here(base - back * STRIDE):
                            base -= back * STRIDE
                            ln += back
                            break
                n = self._probe_run(base)
                if n >= MIN_RUN:
                    self._pool = dict(base=base, count=n, vt=vt, via="scan",
                                      region=r["name"] or "anon",
                                      scan_s=round(time.time() - t0, 1))
                    self._save_state(dict(base=base, count=n, region=r["name"] or "anon",
                                          region_size=r["size"]))
                    self.log("[池] 扫到 %#x, %d 个 (第 %d/%d 段, %.1fs)"
                             % (base, n, i + 1, len(cands), time.time() - t0))
                    return self._pool
            if i < 6 or i % 20 == 0:
                self.log("[池] 第 %d/%d 段 %#x %.1fMB 无命中 (%.1fs)"
                         % (i + 1, len(cands), r["start"], r["size"] / 1048576,
                            time.time() - t0))
        self.log("[池] 没找到（%.1fs）" % (time.time() - t0))
        return None

    # ---------------- 读建筑池 ----------------
    def snapshot(self, pool=None):
        """返回建筑列表，每项 dict(name, level, x, y, village, addr, tid, maxlvl)"""
        p = pool or self.locate()
        if not p:
            return []
        n = p["count"]
        blob = b""
        # 分批读，单批 ≤ 256KB（约 0.3s）
        batch = max(1, (1 << 18) // STRIDE)
        for i in range(0, n, batch):
            k = min(batch, n - i)
            chunk = self.read(p["base"] + i * STRIDE, k * STRIDE)
            if len(chunk) < k * STRIDE:
                chunk = chunk + b"\0" * (k * STRIDE - len(chunk))
            blob += chunk
        out = []
        for i in range(n):
            o = i * STRIDE
            (vt, _p8, _p10, tp) = struct.unpack_from("<4Q", blob, o)
            if vt != p["vt"] or tp == 0:
                continue
            x = struct.unpack_from("<H", blob, o + OFF_X)[0] / 256.0
            y = struct.unpack_from("<H", blob, o + OFF_Y)[0] / 256.0
            # +0x160 是 0 基等级：显示等级（游戏面板上的 N 级）= 字段 + 1
            # 证据：Barracks 字段 13 ↔ 面板「训练营（14级）」；Town Hall 字段 11 ↔ TH12
            lv = struct.unpack_from("<I", blob, o + OFF_LEVEL)[0] + 1
            vil = blob[o + OFF_VILLAGE]
            idx = struct.unpack_from("<I", blob, o + OFF_IDX)[0] - 0x1DCD6500
            ti = self.type_info(tp)
            if not ti.get("building"):
                continue
            out.append(dict(name=ti["name"], tid=ti["tid"], type_ptr=hex(tp),
                            maxlvl=ti["nlevels"], level=lv, raw_level=lv - 1,
                            x=x, y=y,
                            village="bb" if vil else "home", idx=idx,
                            addr=hex(p["base"] + o)))
        return out

    # ---------------- 类型 ----------------
    def _load_type_cache(self):
        if self._types:
            return
        try:
            d = json.load(open(TYPES))
        except Exception:
            d = {}
        if d.get("_libg") == self.libg_base():
            # 只收 tid 合理的条目：重载瞬间读到半初始化的类型对象时，
            # 名字/tid 会是垃圾，那种条目不能留在缓存里
            self._types = {int(k, 16): v for k, v in d.get("types", {}).items()
                           if TID_LO <= (v.get("tid") or 0) < TID_HI and v.get("name")}

    def _save_type_cache(self):
        d = {k: v for k, v in self._types.items()
             if v.get("name") and TID_LO <= (v.get("tid") or 0) < TID_HI}
        try:
            json.dump({"_libg": self.libg_base(),
                       "types": {("%x" % k): v for k, v in d.items()}},
                      open(TYPES, "w"), indent=0)
        except Exception:
            pass

    @staticmethod
    def _plausible_ptr(v):
        return 0x4000000000 <= v < 0x8000000000 and v % 8 == 0

    def type_info(self, tp):
        if not self._types:
            self._load_type_cache()
        if tp in self._types:
            return self._types[tp]
        b = self.read(tp, 0x1A0)
        info = dict(name=None, tid=None, a=None, b=None, nlevels=None)
        if len(b) >= 0x40:
            ln = struct.unpack_from("<I", b, 0x08)[0]
            data = b[0x10:0x28]
            if 0 < ln < 64 and all(32 <= c < 127 for c in data[:ln]):
                info["name"] = data[:ln].decode("utf8", "replace")
            elif 0 < ln < 64:
                p = struct.unpack_from("<Q", b, 0x10)[0]
                s2 = self.read(p, ln)
                if len(s2) >= ln:
                    info["name"] = s2[:ln].decode("utf8", "replace")
            info["tid"] = struct.unpack_from("<I", b, 0x18)[0]
            info["a"] = struct.unpack_from("<I", b, 0x20)[0]
            info["b"] = struct.unpack_from("<I", b, 0x30)[0]
        if len(b) >= 0xA0:
            p98 = struct.unpack_from("<Q", b, 0x98)[0]
            n = 0
            if self._plausible_ptr(p98):
                arr = self.read(p98, 8 * 40)
                for k in range(min(40, len(arr) // 8)):
                    (v,) = struct.unpack_from("<Q", arr, k * 8)
                    if not self._plausible_ptr(v):
                        break
                    n += 1
            info["nlevels"] = n
        info["building"] = bool(info["name"]) and TID_LO <= (info["tid"] or 0) < TID_HI
        self._types[tp] = info
        return info

    # ---------------- 坐标 ----------------
    @staticmethod
    def to_calib(x, y):
        """网格坐标 → 标定视图屏幕坐标"""
        return (PROJ_M[0][0] * x + PROJ_M[0][1] * y + PROJ_T[0],
                PROJ_M[1][0] * x + PROJ_M[1][1] * y + PROJ_T[1])

    def screen(self, x, y, T):
        """网格坐标 → 当前画面坐标（T = coc_backend_mem.calibrate 解出的 {s,tx,ty}）"""
        cx, cy = self.to_calib(x, y)
        if not T:
            return cx, cy
        return T["s"] * cx + T["tx"], T["s"] * cy + T["ty"]

    # ---------------- 价格 ----------------
    def _load_prices(self):
        try:
            return json.load(open(PRICES))
        except Exception:
            return {}

    def close(self):
        self._save_type_cache()
        try:
            self.s.close()
        except Exception:
            pass

    def record_price(self, name, level, currency, cost, source="panel"):
        """把实测到的升级价格写进 building_prices.json（成功后扣费金额就是真价）。"""
        d = self._load_prices()
        d["%s|%d" % (name, level)] = dict(currency=currency, cost=int(cost),
                                          source=source, at=now())
        self._price_cache = d
        try:
            json.dump(d, open(PRICES, "w"), indent=0, ensure_ascii=False)
        except Exception:
            pass
        return d

    def price_trusted(self, name, level):
        """表里这个价格能不能当成准数：红字读出来的（source 以 _red 结尾）只是参考值"""
        pr = self.price(name, level)
        if not pr:
            return False
        return not str(pr.get("source", "")).endswith("_red")

    def price(self, name, level):
        """(类型名, 当前等级) → dict(currency, cost) 或 None。来自 building_prices.json"""
        key = "%s|%d" % (name, level)
        v = self._price_cache.get(key)
        return v if isinstance(v, dict) else None


# ---------------------------------------------------------------- 目标筛选
# 升级货币（默认按 CoC 规则；第一次真正升级后由 building_prices.json 里的实测值覆盖）
# 注意这个是「交叉」的：金矿/储金罐/暗黑钻井花的是**圣水**，圣水收集器/储水瓶花的是**金币**。
# 改之前把资源建筑全归 elixir，结果「金币建筑」只剩防御，白丢了好几个便宜的施工位。
# 面板实测依据（work/probe_*.png，2026-11-21 16:53）：
#   Gold Mine L14 = 1,200,000 圣水   Elixir Collector L15 = 2,000,000 金币
#   Gold Storage L13 = 2,800,000 圣水 Elixir Storage L13 = 2,800,000 金币
#   Army Camp L10 = 7,000,000 圣水    Laboratory L10 = 5,500,000 圣水
CURRENCY_DEFAULT = {
    "elixir": ("Barracks", "Dark Barracks", "Army Camp", "Laboratory",
               "Spell Factory", "Dark Spell Factory", "Siege Workshop",
               "Blacksmith", "Pet House", "Crafting Station",
               "Gold Mine", "Gold Storage", "Dark Elixir Drill", "Dark Elixir Storage",
               "BB Barracks", "BB Army Camp", "BB Star Laboratory",
               "BB Elixir Storage", "BB Gold Mine", "BB Elixir Collector",
               "BB Reinforcement Camp", "BB Gold Storage", "BB Gem Mine"),
    "dark": ("BB Gem Mine",),
}
# 明确不参与自动升级的：没有等级 / 特殊建筑
NO_UPGRADE = ("Helper Hut", "Crafting Station", "BB Reinforcement Camp",
              "BB Battle Machine Altar", "Builders Hut", "BB Army Camp")

# 永远不参与「建筑本体升级」的（用户红线）：大本营 / 部落城堡 / 英雄建筑 / 宠物屋 /
# 铁匠铺 / 实验室本体 / 法术工厂本体。
# 这些是长期投资、占工人、升错一个就毁了整个号，所以即使很便宜也不碰。
#
# 注意：这份名单只拦「建筑本体升级」，**不拦研究**。在实验室里研究兵种/法术是另一条
# 路径（coc_mem_research），它不占工人、只有 1 个槽位、界面和校验完全不同。
# 两件事必须分开：以前实验室只在这一个名单里，导致「实验室不能升」被误读成「不能研究」。
NEVER_UPGRADE = ("Town Hall", "Clan Castle", "Hero Hall", "Laboratory",
                 "Spell Factory", "Dark Spell Factory", "Pet House", "Blacksmith",
                 "BB Battle Machine", "BB Battle Copter", "BB Builder Hall")

# 研究走 coc_mem_research：实验室 / 法术工厂 / 暗黑法术工厂里的「研究对象」不受
# NEVER_UPGRADE 约束（建筑本体照样不许升）。
RESEARCH_BUILDINGS = ("Laboratory", "Spell Factory", "Dark Spell Factory")


def currency_of(name):
    if name in CURRENCY_DEFAULT["elixir"]:
        return "elixir"
    if name in CURRENCY_DEFAULT["dark"]:
        return "dark"
    return "gold"


def load_blocked():
    try:
        return json.load(open(BLOCKED))
    except Exception:
        return {}


def note_blocked(name, level, reason=""):
    """记录「这个类型/等级被游戏挡住」（TH 上限、前置未满足等），以后不再尝试。"""
    d = load_blocked()
    d["%s|%d" % (name, level)] = dict(reason=reason, at=now())
    try:
        json.dump(d, open(BLOCKED, "w"), indent=0)
    except Exception:
        pass
    return d


def discover_upgrade_targets(bld=None, res=None, T=None, currency="gold",
                             max_cost=None, max_cost_by_currency=None,
                             include_bb=False, include_walls=True,
                             include=(), exclude=(), allow_unknown_price=True,
                             max_level=None, min_level=1, skip_blocked=True,
                             tap_dy=None, tap_dx=None, log=None):
    """枚举建筑 → 过滤 → 排序，返回可升级目标列表。

    参数
      res          dict(gold, elixir, dark)；None 表示不按资源过滤
      T            coc_backend_mem.calibrate 的 {s,tx,ty}；None 返回标定视图坐标
      currency     'gold'/'elixir'/'dark'/'any'
      max_cost     单个升级价格上限（需要 price 已知）
      max_cost_by_currency  {币种: 上限}，给了就按币种各管各的（优先于 max_cost）
      include_bb   是否包含夜世界
      include_walls 是否包含城墙
      include/exclude 类型名白/黑名单
      min_level    等级下限（默认 1）
      tap_dy       强制指定竖向偏移；None = 按类型从 tap_offsets.json / DY_PRIOR 取
      tap_dx       强制指定横向偏移；None 同上（投影点偏了的类型用，比如迫击炮/法术工厂）
      skip_blocked 跳过 building_blocked.json 里记过的 (类型,等级)
    返回按「价格（已知的优先）→ 等级 → 类型」排序的列表

    资源过滤只有一条「够不够这一笔」（cost > res[cur] 就跳过）。没有「留多少备用金」
    之类的下限：钱够花就花，目标是别让工人闲着。
    """
    log = log or (lambda *a: None)
    b = bld or Buildings()
    pool = b.locate()
    if not pool:
        log("没定位到建筑池")
        return []
    rows = b.snapshot(pool)
    blocked = load_blocked() if skip_blocked else {}
    offs = load_offsets()
    out = []
    for r in rows:
        nm = r["name"]
        if nm in NO_UPGRADE or nm in NEVER_UPGRADE:
            continue
        if not include_bb and r["village"] == "bb":
            continue
        if not include_walls and nm == "Wall":
            continue
        if include and nm not in include:
            continue
        if exclude and nm in exclude:
            continue
        nlv = r["maxlvl"] or 0
        if nlv < 2:                       # 这个类型本来就没有等级
            continue
        if r["level"] < min_level:        # 等级下限（默认 1 = 最低级）
            continue
        if r["level"] >= nlv:             # 已满级（全局意义上）
            continue
        if max_level and r["level"] >= max_level:
            continue
        if blocked.get("%s|%d" % (nm, r["level"])):
            continue
        pr = b.price(nm, r["level"])
        # 面板实测的币种优先于静态表（实测才是真的：比如某些防御其实花圣水）
        cur = (pr.get("currency") if pr else None) or currency_of(nm)
        if currency and currency != "any" and cur != currency:
            continue
        cost = pr["cost"] if pr else None
        if cost is None and not allow_unknown_price:
            continue
        cap = None
        if max_cost_by_currency:
            cap = max_cost_by_currency.get(cur)
        elif max_cost:
            cap = max_cost
        if cost is not None and cap and cost > cap:
            continue
        if cost is not None and res and cur in res and cost > res[cur]:
            continue
        # 点击点：投影出来的是建筑「脚部/气泡」位置，点它会收资源；
        # 往建筑本体挪（默认 +28，实测大多数类型如此）才会弹信息面板。
        # 横向偶尔也偏（迫击炮 / 法术工厂），所以偏移是 (dx, dy) 一对。
        odx, ody = tap_off_of(nm, offs)
        dx = odx if tap_dx is None else tap_dx
        dy = ody if tap_dy is None else tap_dy
        cx, cy = b.to_calib(r["x"], r["y"])
        sx, sy = b.screen(r["x"], r["y"], T)
        cx, cy, sx, sy = cx + dx, cy + dy, sx + dx, sy + dy
        out.append(dict(name=nm, level=r["level"], maxlvl=nlv,
                        x=r["x"], y=r["y"],
                        calib=[round(cx, 1), round(cy, 1)],
                        screen=[round(sx, 1), round(sy, 1)],
                        currency=cur, price=cost, price_known=cost is not None,
                        tap_dx=dx, tap_dy=dy,
                        tid=r["tid"], idx=r["idx"], village=r["village"],
                        addr=r["addr"]))
    out.sort(key=lambda t: (t["price"] is None, t["price"] or 0,
                            t["level"], t["name"]))
    return out


# ---------------------------------------------------------------- CLI
def _fmt_inv(rows):
    from collections import Counter, defaultdict
    per = defaultdict(list)
    for r in rows:
        per[r["name"]].append(r)
    lines = []
    home = [r for r in rows if r["village"] == "home"]
    bb = [r for r in rows if r["village"] == "bb"]
    lines.append("总计 %d 个建筑：家乡 %d / 夜世界 %d，类型 %d 种"
                 % (len(rows), len(home), len(bb), len(per)))
    for nm in sorted(per, key=lambda n: (-len(per[n]), n)):
        rs = per[nm]
        lv = Counter(r["level"] for r in rs)
        lines.append("  %-24s n=%-4d 等级 %s" % (
            nm, len(rs), " ".join("%s×%d" % (k, v) for k, v in sorted(lv.items()))))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--targets", action="store_true")
    ap.add_argument("--locate", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--probe", default=None, help="打印名字里含这个词的建筑")
    ap.add_argument("--max-cost", type=int, default=None)
    ap.add_argument("--currency", default="any")
    ap.add_argument("--bb", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    b = Buildings(log=print if a.verbose else None)
    t0 = time.time()
    pool = b.locate(force=a.force)
    if not pool:
        print("没定位到建筑池")
        return 1
    print("池 %#x  %d 个  stride %#x  vt %#x  策略 %s  %.1fs"
          % (pool["base"], pool["count"], STRIDE, pool["vt"], pool["via"],
             time.time() - t0))
    if a.locate:
        b.close()
        return 0
    rows = b.snapshot(pool)
    if a.probe:
        key = a.probe.upper().replace("_", " ")
        for r in rows:
            if key in r["name"].upper():
                print("  %-20s lv%-3s %-15s 世界(%5.1f,%5.1f) idx=%-4s %s"
                      % (r["name"], r["level"], r["village"], r["x"], r["y"],
                         r["idx"], r["addr"]))
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    if a.inv:
        print(_fmt_inv(rows))
    if a.targets:
        res = mem.read_res()
        T = (mem.load_calib() or {}).get("T") or mem.load_calib()
        tg = discover_upgrade_targets(b, res=res, T=T, currency=a.currency,
                                      max_cost=a.max_cost, include_bb=a.bb,
                                      log=print)
        print("\n可升级候选 %d 个（资源 %s，货币 %s）" % (len(tg), res, a.currency))
        for t in tg[:60]:
            print("  %-22s lv%-3d/%-3d 世界(%5.1f,%5.1f) 屏(%6.1f,%6.1f) %s"
                  % (t["name"], t["level"], t["maxlvl"], t["x"], t["y"],
                     t["screen"][0], t["screen"][1],
                     ("%s %d" % (t["currency"], t["price"])) if t["price_known"]
                     else "%s ?" % t["currency"]))
    b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
