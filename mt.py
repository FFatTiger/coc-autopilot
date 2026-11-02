#!/usr/bin/env python3
"""minitouch 客户端：点按 / 长按 / 滑动 / 双指，带屏幕旋转坐标映射。

用法:
    from mt import Minitouch
    t = Minitouch()          # 自动读设备尺寸与旋转
    t.tap(540, 1110)
    t.long(540, 1110, 800)
    t.swipe(200, 1800, 200, 400, 300)
    t.pinch(...)
"""
import os
import socket
import struct
import subprocess
import time

SERIAL = os.environ.get("ANDROID_SERIAL", "").strip()
TCP_PORT = 1111


def _adb(*args, timeout=15):
    return subprocess.run(["adb", "-s", SERIAL, *args],
                          capture_output=True, text=True, timeout=timeout)


class Minitouch:
    def __init__(self, port=TCP_PORT, serial=SERIAL):
        self.serial = serial
        self.port = port
        self.sock = None
        self.max_contacts = 10
        self.raw_max_x = 4095
        self.raw_max_y = 4095
        self.screen_w = None
        self.screen_h = None
        self.rotation = 0
        self._connect()
        self.refresh_geometry()

    # ---------- 连接 ----------
    def _ensure_daemon(self):
        out = _adb("shell", "ps -A | grep -c '[m]initouch'").stdout.strip()
        if out in ("", "0"):
            _adb("shell", "nohup /data/local/tmp/minitouch >/data/local/tmp/mt.log 2>&1 </dev/null &")
            time.sleep(1.5)
        _adb("forward", f"tcp:{self.port}", "localabstract:minitouch")

    def _connect(self):
        last = None
        for attempt in range(3):
            try:
                self._ensure_daemon()
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                self.sock.settimeout(3)
                buf = b""
                while buf.count(b"\n") < 2:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise RuntimeError("minitouch 握手失败: " + repr(buf))
                    buf += chunk
                break
            except Exception as e:          # daemon 挂了就杀掉重起
                last = e
                try:
                    self.sock and self.sock.close()
                except Exception:
                    pass
                self.sock = None
                _adb("shell", "pkill -f '[m]initouch'")
                time.sleep(1.2 + attempt * 0.8)
        else:
            raise RuntimeError(f"minitouch 连接失败: {last}")
        lines = buf.decode(errors="replace").strip().split("\n")
        for ln in lines:
            if ln.startswith("^"):
                parts = ln.split()
                self.max_contacts = int(parts[1])
                self.raw_max_x = int(parts[2])
                self.raw_max_y = int(parts[3])
        self.banner = lines

    # ---------- 几何 ----------
    def refresh_geometry(self):
        """读当前逻辑分辨率与旋转（0=竖屏, 1/3=横屏）。"""
        out = _adb("shell", "dumpsys window displays | grep -m1 'cur='").stdout
        # 形如: init=1440x2960 640dpi base=1080x2220 480dpi cur=1080x2220 app=...
        cur = None
        for tok in out.split():
            if tok.startswith("cur="):
                cur = tok[4:]
                break
        if cur:
            w, h = cur.split("x")
            self.screen_w, self.screen_h = int(w), int(h)
        out2 = _adb("shell", "dumpsys display | grep -m1 'mCurrentOrientation'").stdout
        try:
            self.rotation = int(out2.split("=")[1].strip())
        except Exception:
            self.rotation = 0
        return self.screen_w, self.screen_h, self.rotation

    def set_screen(self, w, h):
        """按真实屏幕方向覆盖 geometry：dumpsys 在游戏全屏横屏时仍报竖屏 cur=1080x2220。"""
        self.screen_w, self.screen_h = int(w), int(h)
        self.rotation = 1 if w > h else 0
        return self.screen_w, self.screen_h, self.rotation

    def to_raw(self, x, y):
        """屏幕坐标 -> 触摸设备 raw 坐标。

        screen_w/screen_h 是当前朝向下的逻辑尺寸（横屏时是 2220x1080）。
        触摸设备固定按自然竖屏上报，所以横屏时需要换算出竖屏坐标。
        """
        W, H, rot = self.screen_w, self.screen_h, self.rotation
        if rot in (0, 2):
            # 竖屏：直接按比例归一化
            ex, ey = (x, y) if rot == 0 else (W - x, H - y)
            rx = int(round(ex * self.raw_max_x / W))
            ry = int(round(ey * self.raw_max_y / H))
        else:
            # 横屏：触摸设备按自然竖屏上报，屏幕 x 对应 raw y，屏幕 y 对应 raw x 的反向
            nx = x / W
            ny = y / H
            if rot == 1:           # ROTATION_90
                rx = int(round(ny * self.raw_max_x))
                ry = int(round((1 - nx) * self.raw_max_y))
            else:                  # ROTATION_270
                rx = int(round((1 - ny) * self.raw_max_x))
                ry = int(round(nx * self.raw_max_y))
        rx = max(0, min(self.raw_max_x, rx))
        ry = max(0, min(self.raw_max_y, ry))
        return rx, ry

    # ---------- 底层指令 ----------
    def _send(self, cmd):
        self.sock.sendall(cmd.encode())

    def _commit(self):
        self._send("c\n")

    def _down(self, contact, x, y, pressure=50):
        rx, ry = self.to_raw(x, y)
        self._send(f"d {contact} {rx} {ry} {pressure}\n")

    def _move(self, contact, x, y, pressure=50):
        rx, ry = self.to_raw(x, y)
        self._send(f"m {contact} {rx} {ry} {pressure}\n")

    def _up(self, contact):
        self._send(f"u {contact}\n")

    # ---------- 高层动作 ----------
    def tap(self, x, y, hold_ms=80):
        """按下并保持 hold_ms 再抬起。hold_ms 太小（<40）Unity 可能丢事件。"""
        self._down(0, x, y)
        self._commit()
        time.sleep(hold_ms / 1000.0)
        self._up(0)
        self._commit()

    def long(self, x, y, hold_ms=1200):
        self.tap(x, y, hold_ms)

    def swipe(self, x1, y1, x2, y2, duration_ms=300, steps=None):
        if steps is None:
            steps = max(4, int(duration_ms / 16))
        self._down(0, x1, y1)
        self._commit()
        dt = duration_ms / 1000.0 / steps
        for i in range(1, steps + 1):
            t = i / steps
            self._move(0, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            self._commit()
            time.sleep(dt)
        self._up(0)
        self._commit()

    def pinch(self, cx, cy, start_gap, end_gap, duration_ms=300, steps=10):
        """双指缩放：start_gap/end_gap 是两指到中心的水平距离。"""
        dt = duration_ms / 1000.0 / steps
        self._down(0, cx - start_gap, cy)
        self._down(1, cx + start_gap, cy)
        self._commit()
        for i in range(1, steps + 1):
            t = i / steps
            g = start_gap + (end_gap - start_gap) * t
            self._move(0, cx - g, cy)
            self._move(1, cx + g, cy)
            self._commit()
            time.sleep(dt)
        self._up(0)
        self._up(1)
        self._commit()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    t = Minitouch()
    print(f"banner={t.banner} screen={t.screen_w}x{t.screen_h} rot={t.rotation}")
    if len(sys.argv) >= 3:
        x, y = int(sys.argv[1]), int(sys.argv[2])
        hold = int(sys.argv[3]) if len(sys.argv) > 3 else 80
        print(f"tap ({x},{y}) -> raw {t.to_raw(x, y)} hold={hold}ms")
        t.tap(x, y, hold)
    t.close()
