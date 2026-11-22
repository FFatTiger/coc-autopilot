#!/usr/bin/env python3
"""升级时长的解析与格式化。

游戏面板上的时长是中文单位串：「2天1小时53分钟」「1天12小时」「45分钟」「13小时」。
采集侧要把它变成秒（写进状态文件、页面算进度），所以解析必须「要么对、要么 None」——
宁可记 null 也不能编一个数，否则页面上的进度条就是假的。

支持：
  中文      2天1小时53分钟 / 1天12小时 / 45分钟 / 13小时 / 2天 / 10秒
  英文      1d 12h / 13h / 45m / 2days 1hour 53minutes / 3min
  冒号      2:01:53（时:分:秒）/ 1:30（分:秒）
缺项随便缺、单位顺序不限、中间可以有空格和逗号（OCR 常吐出来）。
不能整串吃完就返回 None（比如夹了别的字，说明这次识别把旁边的字也读进来了）。
"""
import re

UNIT_S = {
    "天": 86400, "日": 86400, "d": 86400,
    "小时": 3600, "时": 3600, "h": 3600,
    "分钟": 60, "分": 60, "m": 60,
    "秒": 1, "s": 1,
}
# 长的写在前面：正则 alternation 是「先撞上谁算谁的」，min 会抢走 minutes
_UNIT_RE = re.compile(r"(天|日|小时|分钟|分钟|时|分|秒|days?|hours?|hrs?|minutes?|mins?|secs?|seconds?|d|h|m|s)",
                      re.IGNORECASE)
_TOKEN_RE = re.compile(r"(\d+)\s*([^\d\s,、]*)")


def _unit_key(u):
    u = (u or "").strip().lower()
    if not u:
        return None
    if u in ("days", "day", "天", "日", "d"):
        return "天"
    if u in ("hours", "hour", "hrs", "hr", "小时", "时", "h"):
        return "小时"
    if u in ("minutes", "minute", "mins", "min", "分钟", "分", "m"):
        return "分钟"
    if u in ("seconds", "second", "secs", "sec", "秒", "s"):
        return "秒"
    return None


def parse_duration(text):
    """「2天1小时53分钟」→ 193980 秒；认不出来返回 None（不猜）。"""
    if text is None:
        return None
    s = str(text)
    if not s.strip():
        return None
    # OCR 会把全角字符/空格吐出来，先归一
    s = (s.replace("：", ":").replace("　", " ")
          .replace("０", "0").replace("１", "1").replace("２", "2").replace("３", "3")
          .replace("４", "4").replace("５", "5").replace("６", "6").replace("７", "7")
          .replace("８", "8").replace("９", "9"))
    s = s.strip()

    # 冒号格式：h:mm:ss / m:ss
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", s.replace(" ", ""))
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is None:
            return int(a) * 60 + int(b)          # m:ss
        return int(a) * 3600 + int(b) * 60 + int(c)

    # 数字 + 单位。允许的噪声：空格、逗号、顿号、末尾的句点
    s2 = s.replace(",", "").replace("，", "").replace("、", "").replace(" ", "")
    total = 0
    seen = 0
    pos = 0
    while pos < len(s2):
        m = _TOKEN_RE.match(s2, pos)
        if not m:
            return None                          # 中间夹了别的字符 → 整串不可信
        num, unit = m.group(1), m.group(2)
        if not unit:
            return None                          # 光有数字没单位（可能是价格数字混进来了）
        um = _UNIT_RE.match(unit)
        if not um:
            return None
        key = _unit_key(um.group(1))
        if key is None:
            return None
        # 单位后面还粘着别的字（「天小时」重叠、或粘了标题字）→ 不可信
        rest = unit[um.end():]
        if rest.strip("."):
            return None
        total += int(num) * UNIT_S[key]
        seen += 1
        pos = m.end()
    if not seen:
        return None
    if total <= 0 or total > 400 * 86400:        # 0 秒 / 一年多 都当认错
        return None
    return total


def fmt_duration(sec, parts=2):
    """秒 → 「2天1小时」（默认只取前两个非零单位，够读就行）"""
    if sec is None:
        return "—"
    try:
        sec = int(round(float(sec)))
    except (TypeError, ValueError):
        return "—"
    if sec <= 0:
        return "0 分钟"
    out = []
    for name, n in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if sec >= n:
            out.append("%d%s" % (sec // n, name))
            sec %= n
        if len(out) >= parts:
            break
    if not out:
        return "%d秒" % max(1, sec)
    return "".join(out)


if __name__ == "__main__":
    import sys
    cases = ["2天1小时53分钟", "1天12小时", "45分钟", "13小时", "2天", "10秒",
             "1d 12h", "13h", "45 minutes", "2天 1小时 53分钟", "升级时间1天12小时",
             "2:01:53", "", None, "2天1小时53", "abc", "0天"]
    bad = 0
    for c in cases:
        got = parse_duration(c)
        print("%-22r → %-8s %s" % (c, got, fmt_duration(got)))
    argv = sys.argv[1:]
    if argv:
        for a in argv:
            print(a, parse_duration(a))
