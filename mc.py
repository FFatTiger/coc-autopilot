#!/usr/bin/env python3
"""minicap 常驻流客户端：拿最新一帧，比 screencap 快一个数量级。

用法:
    from mc import Minicap
    c = Minicap()
    frame = c.grab()          # numpy BGR (h, w, 3)
    c.save('/tmp/x.png')
    c.restart(rotation=1)     # 游戏切横屏后重启
"""
import os
import socket
import struct
import subprocess
import time

import numpy as np

SERIAL = os.environ.get("ANDROID_SERIAL", "").strip()
TCP_PORT = 1313
HDR = struct.Struct("<BBIIIIIBB")


def _adb(*args, timeout=20):
    return subprocess.run(["adb", "-s", SERIAL, *args],
                          capture_output=True, text=True, timeout=timeout)


class Minicap:
    def __init__(self, port=TCP_PORT, quality=60, rate=15, rotation=None, connect=True):
        self.serial = SERIAL
        self.port = port
        self.quality = quality
        self.rate = rate
        self.rotation = rotation
        self.sock = None
        self.width = self.height = None
        if connect:
            self.ensure()
            self.connect()

    # ---------- 守护进程 ----------
    def _probe_display(self):
        """返回 (logical_w, logical_h, rotation)。

        cur= 已经是旋转后的逻辑尺寸（横屏时 2220x1080），minicap 用它直接投影。
        """
        out = _adb("shell", "dumpsys window displays | grep -m1 'cur='").stdout
        cur = None
        for tok in out.split():
            if tok.startswith("cur="):
                cur = tok[4:]
                break
        w, h = (int(v) for v in cur.split("x"))
        o = _adb("shell", "dumpsys display | grep -m1 'mCurrentOrientation'").stdout
        rot = int(o.split("=")[1].strip()) if "=" in o else 0
        return w, h, rot

    def _running(self):
        return _adb("shell", "ps -A | grep -c '[m]inicap'").stdout.strip() not in ("", "0")

    def ensure(self, rotation=None):
        """确保 minicap 常驻进程在跑，投影尺寸跟随当前逻辑分辨率。"""
        w, h, dev_rot = self._probe_display()
        if self._running() and self.width == w and self.height == h:
            return
        _adb("shell", "pkill -f '[m]inicap'")
        time.sleep(0.6)
        proj = f"{w}x{h}@{w}x{h}/0"
        self.width, self.height = w, h
        cmd = (f"nohup env LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap "
               f"-P {proj} -Q {self.quality} -S -r {self.rate} "
               f">/data/local/tmp/mc.log 2>&1 </dev/null &")
        _adb("shell", cmd)
        time.sleep(2.5)
        _adb("forward", f"tcp:{self.port}", "localabstract:minicap")

    def restart(self, rotation=None):
        self.close()
        self.width = self.height = None
        self.ensure(rotation=rotation)
        self.connect()

    # ---------- 流 ----------
    def connect(self):
        if self.sock:
            self.close()
        _adb("forward", f"tcp:{self.port}", "localabstract:minicap")
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.sock.settimeout(5)
        buf = b""
        while len(buf) < 24:
            d = self.sock.recv(4096)
            if not d:
                raise RuntimeError("minicap 握手失败")
            buf += d
        ver, hsize, pid, rw, rh, vw, vh, orient, quirks = HDR.unpack(buf[:24])
        self.version = ver
        self.real = (rw, rh)
        self.virt = (vw, vh)
        self.width, self.height = vw, vh
        self._buf = buf[24:]
        return self

    def _read_frame(self):
        buf = self._buf
        while len(buf) < 4:
            d = self.sock.recv(65536)
            if not d:
                raise RuntimeError("minicap 流关闭")
            buf += d
        ln = struct.unpack("<I", buf[:4])[0]
        buf = buf[4:]
        while len(buf) < ln:
            d = self.sock.recv(max(65536, ln - len(buf)))
            if not d:
                raise RuntimeError("minicap 流关闭")
            buf += d
        self._buf = buf[ln:]
        return buf[:ln]

    def grab(self, decode=True, fresh=True):
        """返回最新一帧。fresh=True 时丢掉积压帧只取最新的。"""
        data = None
        if fresh:
            # 非阻塞地把缓冲区里已有的帧全部读完，取最后一帧
            self.sock.setblocking(False)
            try:
                while True:
                    d = self.sock.recv(1 << 20)
                    if not d:
                        break
                    self._buf += d
            except (BlockingIOError, socket.error):
                pass
            finally:
                self.sock.setblocking(True)
            self.sock.settimeout(5)
            # 解析所有完整帧
            buf = self._buf
            while True:
                if len(buf) < 4:
                    break
                ln = struct.unpack("<I", buf[:4])[0]
                if len(buf) < 4 + ln:
                    break
                data = buf[4:4 + ln]
                buf = buf[4 + ln:]
            self._buf = buf
        if data is None:
            data = self._read_frame()
        if not decode:
            return data
        import cv2
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def save(self, path):
        img = self.grab()
        import cv2
        cv2.imwrite(path, img)
        return img.shape

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


if __name__ == "__main__":
    import sys
    c = Minicap()
    print(f"real={c.real} virt={c.virt}")
    t0 = time.time()
    img = c.grab()
    t1 = time.time()
    print(f"grab {img.shape} in {int((t1 - t0) * 1000)}ms")
    t0 = time.time()
    img = c.grab()
    t1 = time.time()
    print(f"grab2 {img.shape} in {int((t1 - t0) * 1000)}ms")
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mc.png"
    import cv2
    cv2.imwrite(out, img)
    print("saved", out)
