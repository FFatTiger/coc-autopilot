#!/usr/bin/env python3
"""部落冲突巡检看板：读 coc_history/ 下的 meta.json，出状态页 + 历史 + 前后截图。

单文件、零依赖（标准库 http.server），供 PM2 常驻。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coc_config
import coc_progress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HIST = os.environ.get("COC_HISTORY") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history")
PORT = int(os.environ.get("COC_PORT") or 8899)   # 同一台机跑第二份看板（比如试新后端）时改这个
MAX_RUNS = 200
BACKEND_LABELS = {"ocr": "OCR 后端", "memory": "内存后端"}

ROOT = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(ROOT, "static", "icons")      # tools/icons_build.py 生成

RUN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coc_patrol_run.py")
PYTHON = os.environ.get("COC_PYTHON") or sys.executable
RUN_LOG = "/tmp/coc_manual_run.log"
_proc = {"p": None, "started": None}

_icons_cache = {"at": 0, "data": {}}


def icon_map():
    """static/icons/*.png → {名字: data URI}，进程内缓存（文件改了 mtime 才重读）。

    内嵌的用意：页面必须离线可开，一个外部请求都不能有（也不走 CDN）。
    """
    try:
        files = sorted(f for f in os.listdir(ICON_DIR) if f.endswith(".png"))
    except OSError:
        return {}
    stamp = 0
    for f in files:
        try:
            stamp = max(stamp, os.path.getmtime(os.path.join(ICON_DIR, f)))
        except OSError:
            pass
    if _icons_cache["data"] and _icons_cache["at"] == stamp:
        return _icons_cache["data"]
    out = {}
    for f in files:
        try:
            with open(os.path.join(ICON_DIR, f), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        out[f[:-4]] = "data:image/png;base64," + base64.b64encode(raw).decode()
    _icons_cache.update(at=stamp, data=out)
    return out


def state_payload():
    """「正在升级」这一块的数据：状态文件里那部分 + 服务器当前时间"""
    cfg = coc_config.load()
    return coc_progress.build(total_builders=cfg.get("builders", 5),
                              build_max_h=cfg.get("build_max_h", 168),
                              research_max_h=cfg.get("research_max_h", 240))


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f2f3f5" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#0f1114" media="(prefers-color-scheme:dark)">
<title>部落冲突 · 巡检看板</title>
<style>
  :root{
    color-scheme:light dark;
    --bg:#f2f3f5; --card:#fff; --surface:#fff; --surface-2:#fafbfc; --surface-3:#f7f8f9;
    --ink:#1b1d21; --ink-2:#6b7280; --ink-3:#9ca3af;
    --line:#eceef1; --gold:#e8a11c; --elixir:#b04ce0; --dark:#4b4257;
    --ok:#16a34a; --bad:#dc2626; --warn:#d97706;
    --ok-bg:#f0fdf4; --ok-bd:#dcfce7;
    --bad-bg:#fef2f2; --bad-bd:#fee2e2;
    --warn-bg:#fffbeb; --warn-bd:#fef3c7;
    --chip-bg:#f3f4f6;
    --info:#4338ca; --info-bg:#e0e7ff; --info-bd:#e0e7ff;
    --accent:#1b1d21; --on-accent:#fff;
    --live-bg:#f0fdf4; --live-bd:#dcfce7; --live-ink:#166534; --live-pre:#fff; --live-spin:#bbf7d0;
    --track:#eef0f3; --track-2:#e3e6ea; --bar:#f0a92c; --bar-2:#f7c65b;
    --lab:#7c3aed; --lab-bg:#f6f2ff; --lab-bd:#e7dcff; --lab-bar:#a78bfa;
    --toast-bg:#1b1d21; --toast-ink:#fff;
    --shadow:0 2px 10px rgba(0,0,0,.22);
    --r:16px; --r-sm:10px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1114; --card:#171a1f; --surface:#1b1f25; --surface-2:#1f242b; --surface-3:#222730;
      --ink:#e7eaee; --ink-2:#a2abb6; --ink-3:#78828e;
      --line:#282d35; --gold:#f0b03a; --elixir:#c47bf0; --dark:#9a92b3;
      --ok:#4ade80; --bad:#f87171; --warn:#fbbf24;
      --ok-bg:#12261b; --ok-bd:#1e3d2b;
      --bad-bg:#2a1517; --bad-bd:#402021;
      --warn-bg:#2a2111; --warn-bd:#3e3119;
      --chip-bg:#222730;
      --info:#a5b4fc; --info-bg:#1e2340; --info-bd:#2c3462;
      --accent:#e7eaee; --on-accent:#14171c;
      --live-bg:#12261b; --live-bd:#1e3d2b; --live-ink:#86efac; --live-pre:#0c130f; --live-spin:#1e3d2b;
      --track:#242a32; --track-2:#2c333c; --bar:#f0b03a; --bar-2:#f7cd6a;
      --lab:#c4b5fd; --lab-bg:#1d1a2b; --lab-bd:#322b48; --lab-bar:#8b5cf6;
      --toast-bg:#e7eaee; --toast-ink:#14171c;
      --shadow:0 2px 12px rgba(0,0,0,.55);
    }
  }
  *,*::before,*::after{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:rgba(0,0,0,.07)}
  body.sheet-open{overflow:hidden}
  img{max-width:100%}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:20px;font-weight:600;margin:0 0 4px}
  .sub{color:var(--ink-2);font-size:13px}
  .topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:22px}
  .topbar .actions{display:flex;gap:8px;flex:none}
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
  .card{background:var(--card);border-radius:var(--r);padding:18px 20px;border:1px solid var(--line)}
  .card .k{color:var(--ink-2);font-size:13px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
  .card .v{font-size:26px;font-weight:600;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
  .card .v small{font-size:14px;color:var(--ink-3);font-weight:400;margin-left:4px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
  .dot.gold{background:var(--gold)} .dot.elixir{background:var(--elixir)} .dot.dark{background:var(--dark)}
  .section{margin-top:26px}
  .section h2{font-size:15px;font-weight:600;margin:0 0 12px;color:var(--ink)}
  .bars{display:flex;align-items:flex-end;gap:6px;height:88px;padding:10px 4px 0}
  .bar{flex:1 1 0;min-width:6px;border-radius:4px 4px 0 0;background:var(--gold);opacity:.85;
       transition:opacity .15s,transform .1s;cursor:pointer}
  .bar.neg{background:var(--ink-3)}
  .bar.on{opacity:1}
  .bars-empty{flex:1;text-align:center;color:var(--ink-3);font-size:13px;padding:26px 0}
  .bars-x{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
          color:var(--ink-3);font-size:11px;margin-top:6px}
  .bars-read{flex:1 1 auto;text-align:center;color:var(--ink-2);font-size:inherit;
             font-variant-numeric:tabular-nums;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;color:var(--ink-2);font-weight:500;font-size:12px;
     padding:0 12px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
  th.col-first,td.col-first{padding-left:20px}
  tbody tr[data-id]{cursor:pointer}
  td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
        background:var(--chip-bg);color:var(--ink-2);border:1px solid var(--line)}
  .pill.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-bd)}
  .pill.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--bad-bd)}
  .pill.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-bd)}
  .up{color:var(--ok)} .down{color:var(--bad)} .flat{color:var(--ink-3)}
  .detail{background:var(--surface-2);border-radius:var(--r-sm);padding:16px;margin:2px 0 12px}
  .detail h3{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--ink-2)}
  .detail-cell{padding:0 12px 14px}
  /* 前后对比：滑动模式（默认桌面）/ 上下两张（默认窄屏） */
  .cmp{position:relative;border-radius:var(--r-sm);overflow:hidden;border:1px solid var(--line);
       background:var(--surface);user-select:none;-webkit-user-select:none;
       -webkit-touch-callout:none;touch-action:none;cursor:ew-resize}
  .cmp .pane{position:relative;margin:0}
  .cmp .pane img{display:block;width:100%;height:auto;object-fit:contain;pointer-events:none;
       -webkit-user-drag:none}
  .cmp .pane.after{position:absolute;inset:0;overflow:hidden;clip-path:inset(0 0 0 50%)}
  .cmp .pane.after img{position:absolute;inset:0;width:100%;height:100%;
       object-fit:contain;object-position:left top}
  .cmp .divider{position:absolute;top:0;bottom:0;left:50%;width:2px;background:#fff;
       box-shadow:0 0 0 1px rgba(0,0,0,.25);pointer-events:none;z-index:2}
  .cmp .knob{position:absolute;top:50%;left:50%;width:38px;height:38px;margin:-19px 0 0 -19px;border-radius:50%;
       background:#fff;box-shadow:var(--shadow);display:flex;align-items:center;
       justify-content:center;font-size:14px;color:#41474f;pointer-events:none;z-index:3}
  .cmp .tag{position:absolute;top:8px;padding:3px 9px;border-radius:999px;font-size:11px;
       background:rgba(17,19,23,.62);color:#fff;pointer-events:none;z-index:2}
  .cmp .tag.l{left:8px} .cmp .tag.r{right:8px}
  .cmp[data-mode="stack"]{display:grid;grid-template-columns:1fr;gap:10px;border:0;background:transparent;
       overflow:visible;touch-action:auto;cursor:default;border-radius:0}
  .cmp[data-mode="stack"] .pane{position:relative;inset:auto;overflow:hidden;clip-path:none;
       border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface)}
  .cmp[data-mode="stack"] .pane.after img{position:static;width:100%;height:auto}
  .cmp[data-mode="stack"] .divider,
  .cmp[data-mode="stack"] .knob,
  .cmp[data-mode="stack"] .tag{display:none}
  .cmp[data-mode="stack"] .pane::after{content:attr(data-cap);display:block;padding:7px 12px;
       font-size:12px;color:var(--ink-2);border-top:1px solid var(--line);background:var(--surface-2)}
  .cmp-bar{display:flex;align-items:center;gap:12px;margin-top:10px;font-size:12px;
           color:var(--ink-3);flex-wrap:wrap}
  .cmp-nums{display:flex;flex-wrap:wrap;gap:4px 14px;flex:1 1 220px;min-width:0;
            font-variant-numeric:tabular-nums}
  .shots{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .shot{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden}
  .shot img{width:100%;display:block}
  .shot .cap{padding:8px 12px;font-size:12px;color:var(--ink-2);border-top:1px solid var(--line)}
  .items{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .item{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12.5px}
  .item.up{border-color:var(--ok-bd);color:var(--ok)}
  .item.res{border-color:var(--info-bd);color:var(--info)}
  .err{color:var(--bad);font-size:13px;margin-top:10px}
  .empty{color:var(--ink-3);padding:28px 0;text-align:center;font-size:14px}
  .hist-card{padding:18px 8px 4px}
  .more-wrap{display:none;padding:6px 12px 14px}
  .btn{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:10px;
        padding:8px 16px;font-size:14px;cursor:pointer;font-family:inherit;
        display:inline-flex;align-items:center;justify-content:center;gap:6px;
        transition:background .12s,transform .06s}
  .btn.primary{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
  .btn.tiny{padding:6px 12px;font-size:12.5px;border-radius:9px;min-height:34px}
  .btn:active{background:var(--surface-2);transform:translateY(1px)}
  .btn.primary:active{opacity:.86}
  .btn:disabled{cursor:default}
  .mask{position:fixed;inset:0;background:rgba(17,19,23,.32);display:none;z-index:50;
         align-items:flex-start;justify-content:center;padding:6vh 16px;overflow:auto}
  .mask.on{display:flex}
  .sheet{background:var(--surface);border-radius:20px;width:100%;max-width:520px;padding:24px 26px 20px;
          border:1px solid var(--line);display:flex;flex-direction:column;
          max-height:88vh;max-height:88dvh}
  .sheet-head{flex:none}
  .sheet-body{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain}
  .sheet h2{font-size:17px;font-weight:600;margin:0 0 4px}
  .sheet .hint{color:var(--ink-2);font-size:13px;margin-bottom:18px}
  .sheet-x{display:none}
  .field{margin-bottom:18px}
  .field label{display:block;font-size:13px;color:var(--ink-2);margin-bottom:7px}
  .field .desc{font-size:12px;color:var(--ink-3);margin-top:6px;line-height:1.5}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{border:1px solid var(--line);background:var(--surface);color:var(--ink-2);border-radius:999px;
         padding:6px 13px;font-size:13px;cursor:pointer;font-family:inherit;
         display:inline-flex;align-items:center;justify-content:center;
         transition:background .12s,color .12s}
  .chip.on{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
  .chip.add{border-style:dashed;color:var(--ink-3)}
  .chip:active{background:var(--surface-2)}
  .chip.on:active{opacity:.86}
  input[type=number],input[type=time]{font:inherit;border:1px solid var(--line);border-radius:10px;
        padding:8px 12px;width:100%;background:var(--surface);color:var(--ink)}
  .times{display:flex;flex-wrap:wrap;gap:8px}
  .time-item{display:flex;align-items:center;gap:6px;background:var(--surface-3);border-radius:10px;
        padding:4px 4px 4px 10px}
  .time-item input{border:0;background:transparent;width:96px;padding:4px 0;
        font-variant-numeric:tabular-nums}
  .time-item button{border:0;background:transparent;color:var(--ink-3);cursor:pointer;font-size:16px;
        line-height:1;padding:4px 8px;border-radius:6px;font-family:inherit}
  .time-item button:active{background:var(--line);color:var(--ink)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .sheet-foot{display:flex;justify-content:flex-end;gap:10px;margin-top:22px;padding-top:18px;
        border-top:1px solid var(--line)}
  .live{display:none;margin-top:14px;border:1px solid var(--live-bd);background:var(--live-bg);
        border-radius:var(--r-sm);padding:12px 16px;font-size:13px;color:var(--live-ink)}
  .live.on{display:block}
  .live .row{display:flex;align-items:center;gap:10px}
  .spinner{width:13px;height:13px;border:2px solid var(--live-spin);border-top-color:var(--ok);
        border-radius:50%;animation:spin .8s linear infinite;flex:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .live pre{margin:8px 0 0;max-height:110px;overflow:auto;font-size:11.5px;line-height:1.5;
        color:var(--live-ink);background:var(--live-pre);border-radius:8px;padding:8px 10px;
        white-space:pre-wrap;word-break:break-all}
  .toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:var(--toast-bg);
         color:var(--toast-ink);padding:10px 18px;border-radius:999px;font-size:14px;opacity:0;
         transition:opacity .2s;z-index:60;pointer-events:none;max-width:calc(100% - 32px);text-align:center}
  .toast.on{opacity:1}

  /* ---------------- 正在升级（在建） ---------------- */
  .upg{margin-top:22px}
  .upg-head{display:flex;align-items:center;justify-content:space-between;gap:12px;
       margin:0 0 11px;flex-wrap:wrap}
  .upg-head h2{margin:0}
  .upg-chips{display:flex;gap:7px;flex-wrap:wrap}
  .kchip{display:inline-flex;align-items:center;gap:5px;padding:3px 10px 3px 7px;border-radius:999px;
       background:var(--chip-bg);color:var(--ink-2);font-size:12px;line-height:1.5;
       font-variant-numeric:tabular-nums;white-space:nowrap}
  .kchip b{color:var(--ink);font-weight:600}
  .kchip img,.kchip svg{width:13px;height:13px;display:block;opacity:.95}
  .upg-list{display:grid;gap:9px}
  .upg-row{display:grid;grid-template-columns:38px minmax(0,1fr) auto;align-items:center;
       gap:14px;background:var(--card);border:1px solid var(--line);border-radius:15px;
       padding:12px 16px 12px 14px;position:relative;overflow:hidden}
  .upg-row.research{border-color:var(--lab-bd);background:var(--lab-bg)}
  .upg-ic{width:38px;height:38px;border-radius:11px;overflow:hidden;flex:none;position:relative;
       background:var(--surface-3);display:grid;place-items:center}
  .upg-ic img{width:100%;height:100%;object-fit:cover;display:block}
  .upg-ic svg{width:22px;height:22px;display:block}
  .upg-ic.fallback{border-radius:11px;font-size:14px;font-weight:600;color:#fff;
       background:linear-gradient(160deg, hsl(var(--h) 64% 60%), hsl(var(--h) 64% 42%));
       display:grid;place-items:center;letter-spacing:-.3px;
       box-shadow:inset 0 1px 0 rgba(255,255,255,.28), inset 0 -2px 6px rgba(0,0,0,.14);
       text-shadow:0 1px 1px rgba(0,0,0,.18)}
  .upg-body{min-width:0}
  .upg-l1{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:7px}
  .upg-name{font-weight:600;font-size:14.5px;letter-spacing:-.1px;white-space:nowrap;
       overflow:hidden;text-overflow:ellipsis;max-width:100%}
  .upg-lv{display:inline-flex;align-items:center;gap:3px;font-size:12px;color:var(--ink-2);
       font-variant-numeric:tabular-nums;background:var(--chip-bg);border-radius:999px;padding:1px 8px}
  .upg-lv b{color:var(--ink);font-weight:600}
  .upg-lv i{color:var(--ink-3);font-style:normal}
  .upg-flag{font-size:11.5px;color:var(--warn);background:var(--warn-bg);
       border:1px solid var(--warn-bd);border-radius:999px;padding:0 7px;line-height:1.55}
  .upg-track-wrap{display:flex;align-items:center;gap:7px;margin:2px 0 8px}
  .upg-track-wrap .cap{flex:none;width:15px;height:15px;opacity:.85;display:grid;place-items:center}
  .upg-track-wrap .cap svg{width:15px;height:15px;display:block}
  .upg-track-wrap .cap.end{opacity:.5}
  .upg-track{position:relative;flex:1 1 auto;height:9px;border-radius:999px;
       background:var(--track);margin:0}
  .upg-row.nodur .upg-track{background-image:repeating-linear-gradient(115deg,
       var(--track-2) 0 6px, transparent 6px 12px);background-color:var(--track)}
  .upg-row.nodur .upg-time b{color:var(--ink-2);font-weight:500;font-size:15px}
  .upg-row.nodur .cap.end{opacity:.28}
  .upg-track .fill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;
       background:linear-gradient(90deg,var(--bar),var(--bar-2));transition:width .6s ease}
  .upg-row.research .upg-track .fill{background:linear-gradient(90deg,var(--lab-bar),var(--lab))}
  .upg-track .fill.done{background:linear-gradient(90deg,var(--ok),#4ade80)}
  .upg-track .knob{position:absolute;top:50%;width:16px;height:16px;border-radius:50%;
       background:var(--card);border:2px solid var(--bar);transform:translate(-50%,-50%);
       box-shadow:0 1px 3px rgba(0,0,0,.18);transition:left .6s ease;z-index:2}
  .upg-row.research .upg-track .knob{border-color:var(--lab-bar)}
  .upg-l2{display:flex;align-items:center;gap:6px 12px;flex-wrap:wrap;font-size:12px;
       color:var(--ink-2);font-variant-numeric:tabular-nums}
  .upg-l2 .k{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
  .upg-l2 img{width:13px;height:13px;display:block}
  .upg-l2 .pct{color:var(--ink);font-weight:600}
  .upg-time{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
  .upg-time b{display:block;font-size:16px;font-weight:600;letter-spacing:-.2px;line-height:1.25}
  .upg-time span{font-size:11.5px;color:var(--ink-3)}
  .upg-row.done{border-color:var(--ok-bd);background:var(--ok-bg)}
  .upg-row.done .upg-time b{color:var(--ok)}
  .upg-empty{display:flex;align-items:center;gap:12px;color:var(--ink-2);font-size:13.5px;
       background:var(--card);border:1px dashed var(--line);border-radius:15px;padding:16px}
  .upg-empty svg{width:22px;height:22px;opacity:.5;flex:none}
  .card .k img.cic{width:17px;height:17px;display:block;margin-top:-1px}
  .card .k svg{width:15px;height:15px;display:block;opacity:.75}
  /* 历史表里的状态：图标 + 极短标注 */
  .st{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--ink-2);white-space:nowrap}
  .st svg{width:16px;height:16px;display:block}
  .st.ok{color:var(--ok)} .st.bad{color:var(--bad)} .st.warn{color:var(--warn)}
  .cur{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
  .cur img{width:15px;height:15px;display:inline-block;vertical-align:-3px}

  @media (hover:hover){
    .bar:hover{opacity:1}
    tbody tr[data-id]:hover td{background:var(--surface-2)}
    .btn:hover{background:var(--surface-2)}
    .btn.primary:hover{opacity:.88}
    .chip:hover{background:var(--surface-2)}
    .chip.on:hover{background:var(--accent);opacity:.92}
    .time-item button:hover{background:var(--line);color:var(--ink)}
  }
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }

  /* ---- 平板：收掉两列不重要的字段，键值对仍留在同一行 ---- */
  @media (max-width:900px){
    .wrap{padding:22px 16px 56px}
    .cards{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
    .card .v{font-size:24px}
    .row2{grid-template-columns:1fr}
    td.hide-md,th.hide-md{display:none}
    /* 平板也是触屏：按钮、开关、输入框按触控目标给足 */
    .btn{min-height:44px}
    .chip{min-height:44px;padding:0 16px}
    .time-item{min-height:46px;padding:2px 2px 2px 12px}
    .time-item button{width:44px;height:44px;display:flex;align-items:center;justify-content:center}
    input[type=number],input[type=time]{font-size:16px;min-height:46px}
  }

  /* ---- 手机：真正换布局，不是缩小版桌面 ---- */
  @media (max-width:640px){
    .wrap{padding:14px 14px calc(36px + env(safe-area-inset-bottom))}
    .topbar{flex-direction:column;align-items:stretch;gap:12px;margin-bottom:16px}
    .topbar .actions{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .btn{min-height:44px;padding:11px 16px;font-size:15px;width:100%}
    .btn.tiny{min-height:40px;font-size:13px}
    h1{font-size:18px;line-height:1.4}
    .sub{font-size:12.5px}
    .cards{grid-template-columns:1fr 1fr;gap:10px}
    .card{padding:14px 14px 12px;border-radius:14px}
    .card .k{font-size:12px;margin-bottom:6px}
    .card .v{font-size:21px;letter-spacing:-.2px}
    .card .v small{font-size:12px}
    .cards > .card:last-child:nth-child(odd){grid-column:1/-1}
    .section{margin-top:20px}
    .section h2{margin-bottom:10px}
    .bars{height:76px;gap:3px;padding:8px 2px 0;overflow:hidden}
    .bar{min-width:0}
    .bars-x{font-size:10.5px}
    .live{padding:10px 12px}
    /* 历史：表格塌成卡片，字段名一行、值一行 */
    .hist-card{padding:0;background:transparent;border:0;border-radius:0}
    table,tbody{display:block;width:100%}
    table thead{display:none}
    tbody tr[data-id]{display:grid;grid-template-columns:1fr 1fr;column-gap:14px;row-gap:1px;
         border:1px solid var(--line);border-radius:12px;background:var(--card);
         padding:11px 13px;margin:0 0 9px;box-shadow:none}
    tbody tr[data-id]:active{background:var(--surface-2)}
    tbody tr.detail-row{display:block;border:0;background:transparent;padding:0;
         margin:-6px 0 10px;border-radius:0}
    tbody tr.detail-row td{display:block;padding:0;border:0}
    td{display:flex;align-items:baseline;justify-content:space-between;gap:8px;
       padding:1.5px 0;border:0;font-size:13.5px;white-space:normal}
    td::before{content:attr(data-label);color:var(--ink-2);font-size:12.5px;font-weight:400;flex:none}
    td.col-first{padding-left:0}
    td.hide-sm,td.hide-md{display:flex}
    th.hide-sm,th.hide-md{display:none}
    td[data-col="time"]{order:1;grid-column:1/2;font-weight:600;font-size:14px;
         justify-content:flex-start;gap:6px;padding-bottom:6px}
    td[data-col="time"]::before{content:none}
    td[data-col="time"]::after{content:"›";color:var(--ink-3);font-size:16px;line-height:1;
         transition:transform .15s}
    tr[data-open] td[data-col="time"]::after{transform:rotate(90deg)}
    td[data-col="status"]{order:2;grid-column:2/3;justify-content:flex-end;padding-bottom:6px}
    td[data-col="status"]::before{content:none}
    td[data-col="gold"]{order:3} td[data-col="elixir"]{order:4}
    td[data-col="dark"]{order:5} td[data-col="workers"]{order:6}
    td[data-col="collected"]{order:7} td[data-col="dur"]{order:8}
    td.num{white-space:normal;font-size:13.5px}
    .more-wrap{padding:2px 0 12px}
    .more-wrap .btn{width:100%}
    .detail{padding:12px;border-radius:12px}
    .cmp-bar{flex-direction:column;align-items:stretch;gap:8px}
    .cmp-nums{flex:0 1 auto;flex-direction:column;gap:2px;font-size:12.5px}
    .shots{grid-template-columns:1fr}
    .item{font-size:12px;padding:5px 10px}
    /* 在建：手机上把「剩余」挪到名字右边那行，进度条整行铺满 */
    .upg{margin-top:18px}
    .upg-head{margin-bottom:9px;gap:8px}
    .upg-chips{gap:6px}
    .kchip{font-size:11.5px;padding:2px 9px 2px 6px}
    .upg-row{grid-template-columns:34px minmax(0,1fr);gap:11px;padding:11px 12px;
         border-radius:13px}
    .upg-ic{width:34px;height:34px;border-radius:10px}
    .upg-l1{margin-bottom:6px;gap:6px}
    .upg-name{font-size:14px}
    .upg-lv{font-size:11.5px;padding:0 7px}
    .upg-time{grid-column:1/-1;display:flex;align-items:baseline;justify-content:space-between;
         gap:8px;margin-top:2px;padding-top:7px;border-top:1px solid var(--line)}
    .upg-row.done .upg-time{border-top-color:var(--ok-bd)}
    .upg-time b{font-size:15px}
    .upg-time span{font-size:11.5px;order:2}
    .upg-row.research .upg-time{border-top-color:var(--lab-bd)}
    .upg-l2{gap:4px 10px;font-size:11.5px}
    .upg-empty{padding:14px;font-size:13px}
    /* 设置：底部抽屉 + 拇指区按钮 + 16px 输入框（防 iOS 自动放大） */
    .mask{align-items:flex-end;justify-content:stretch;padding:0}
    .sheet{max-width:none;width:100%;border-radius:20px 20px 0 0;border-bottom:0;
           padding:16px 16px 0;max-height:92vh;max-height:92dvh;display:flex;flex-direction:column}
    .sheet-head{position:relative;flex:none;padding-right:44px}
    .sheet h2{font-size:16.5px}
    .sheet .hint{font-size:12.5px;margin-bottom:12px}
    .sheet-body{flex:1 1 auto;overflow-y:auto;-webkit-overflow-scrolling:touch;
           overscroll-behavior:contain;padding:4px 0 6px;min-height:0}
    .sheet-x{display:flex;position:absolute;right:0;top:-4px;width:40px;height:40px;
           align-items:center;justify-content:center;border:0;background:var(--surface-3);
           color:var(--ink-2);border-radius:50%;font-size:20px;line-height:1;cursor:pointer;
           font-family:inherit}
    .sheet-x:active{background:var(--line)}
    .field{margin-bottom:16px}
    .field label{font-size:13px}
    .field .desc{font-size:12px}
    .chips{gap:8px}
    .chip{min-height:44px;padding:0 16px;font-size:14px}
    .chips .chip{flex:0 1 auto}
    input[type=number],input[type=time]{font-size:16px;min-height:46px;padding:9px 12px}
    .times{gap:8px}
    .time-item{flex:1 1 140px;min-height:46px;padding:2px 2px 2px 12px}
    .time-item input{width:auto;flex:1 1 auto;min-width:0;font-size:16px}
    .time-item button{width:44px;height:44px;font-size:20px;flex:none;display:flex;
           align-items:center;justify-content:center}
    .sheet-foot{flex:none;position:sticky;bottom:0;background:var(--surface);margin:0 -16px;
           padding:12px 16px calc(12px + env(safe-area-inset-bottom));
           border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .sheet-foot .btn{min-height:46px;font-size:15px}
    .toast{bottom:calc(24px + env(safe-area-inset-bottom));font-size:13.5px}
  }
  @media (max-width:380px){
    .card .v{font-size:19px}
    .wrap{padding-left:11px;padding-right:11px}
    td{font-size:13px}
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>部落冲突 · 巡检看板 <span class="pill" id="bkBadge" title="当前读数后端">__BACKEND_LABEL__</span></h1>
      <div class="sub" id="sub">加载中…</div>
    </div>
    <div class="actions">
      <button class="btn primary" id="btn-run" type="button">立即运行</button>
      <button class="btn" id="btn-cfg" type="button">设置</button>
    </div>
  </div>

  <div class="grid cards" id="cards"></div>

  <section class="section upg" id="upgSec">
    <div class="upg-head">
      <h2>正在升级</h2>
      <div class="upg-chips" id="upgChips"></div>
    </div>
    <div class="upg-list" id="upgList"></div>
  </section>

  <div class="live" id="live">
    <div class="row"><span class="spinner"></span><span id="liveText">正在运行巡检…</span></div>
    <pre id="liveLog"></pre>
  </div>

  <div class="section">
    <h2>最近每轮金币收益</h2>
    <div class="card">
      <div class="bars" id="bars"></div>
      <div class="bars-x" id="barsx"></div>
    </div>
  </div>

  <div class="section">
    <h2>历史记录</h2>
    <div class="card hist-card">
      <table>
        <thead><tr>
          <th class="col-first">时间</th>
          <th class="hide-sm">金币</th><th class="hide-sm">圣水</th><th class="hide-sm">暗黑</th>
          <th>工人</th><th class="hide-sm hide-md">收集</th><th class="hide-sm hide-md">耗时</th><th>状态</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="more-wrap" id="moreWrap">
        <button class="btn" id="moreRows" type="button">加载更多</button>
      </div>
      <div class="empty" id="none" style="display:none">还没有巡检记录，等第一轮跑完就有了。</div>
    </div>
  </div>
</div>

<div class="mask" id="mask">
  <div class="sheet" role="dialog" aria-modal="true" aria-label="设置">
    <div class="sheet-head">
      <h2>设置</h2>
      <div class="hint">按北京时间。保存后下一轮巡检即生效，不需要重启服务。</div>
      <button class="sheet-x" id="btn-x" type="button" aria-label="关闭">×</button>
    </div>
    <div class="sheet-body">

    <div class="field">
      <label>巡检时间</label>
      <div class="times" id="times"></div>
      <div class="desc">每天在这些时刻跑一轮。</div>
    </div>

    <div class="field">
      <label>读数后端</label>
      <div class="chips">
        <button class="chip" data-backend="ocr" type="button">OCR（截图识别）</button>
        <button class="chip" data-backend="memory" type="button">内存（root 读内存）</button>
      </div>
      <div class="desc">OCR 兼容性好、不需要 root，但数字会认错位、增量只能靠投票估；
        内存逐位精确、地址失效能自愈，但要求手机 root 并且要先定位一次地址。
        保存后下一轮巡检生效。</div>
    </div>

    <div class="field">
      <label>收资源</label>
      <div class="chips">
        <button class="chip" data-toggle="collect" data-val="1" type="button">开启</button>
        <button class="chip" data-toggle="collect" data-val="0" type="button">关闭</button>
      </div>
    </div>

    <div class="field">
      <label>自动升级</label>
      <div class="chips">
        <button class="chip" data-toggle="upgrade" data-val="1" type="button">开启</button>
        <button class="chip" data-toggle="upgrade" data-val="0" type="button">关闭</button>
      </div>
      <div class="desc">关闭后只收集和巡检，不花任何资源。</div>
    </div>

    <div class="field">
      <label>每轮冷启动游戏</label>
      <div class="chips">
        <button class="chip" data-toggle="cold_start" data-val="1" type="button">开启</button>
        <button class="chip" data-toggle="cold_start" data-val="0" type="button">关闭</button>
      </div>
      <div class="desc">开启后每轮先唤醒解锁、没跑就拉起游戏、等进村再干活（内存地址会失效，要重新定位一次，单轮约 3~5 分钟）。</div>
    </div>

    <div class="field">
      <label>跑完关掉游戏</label>
      <div class="chips">
        <button class="chip" data-toggle="close_game_after_run" data-val="1" type="button">开启</button>
        <button class="chip" data-toggle="close_game_after_run" data-val="0" type="button">关闭</button>
      </div>
      <div class="desc">记录落盘之后再杀进程（先杀就拿不到截图和数据）。关掉游戏能省屏幕、降温度。</div>
    </div>

    <div class="field">
      <label>跑完锁屏</label>
      <div class="chips">
        <button class="chip" data-toggle="lock_screen_after_run" data-val="1" type="button">开启</button>
        <button class="chip" data-toggle="lock_screen_after_run" data-val="0" type="button">关闭</button>
      </div>
      <div class="desc">锁完会回读 dumpsys 确认真的睡了（mWakefulness=Asleep）；下一轮自己唤醒解锁。</div>
    </div>

    <div class="field">
      <label>允许花费的资源</label>
      <div class="chips" id="kinds">
        <button class="chip" data-kind="gold" type="button">金币</button>
        <button class="chip" data-kind="elixir" type="button">圣水</button>
        <button class="chip" data-kind="dark" type="button">暗黑重油</button>
      </div>
      <div class="desc">暗黑重油比较稀有，默认关闭。</div>
    </div>

    <div class="row2">
      <div class="field">
        <label>单次升级价格上限</label>
        <input type="number" id="maxPrice" min="0" max="50000000" step="100000" inputmode="numeric">
        <div class="desc">超过这个价格的项目会跳过。</div>
      </div>
      <div class="field">
        <label>每轮最多升几个</label>
        <input type="number" id="maxUp" min="0" max="5" step="1" inputmode="numeric">
        <div class="desc">0 表示不升级。</div>
      </div>
    </div>

    <div class="field">
      <label>至少保留的空闲工人</label>
      <input type="number" id="minFree" min="0" max="5" step="1" inputmode="numeric">
      <div class="desc">留 1 表示始终给手动操作留一个工人；填 0 表示有多少用多少。</div>
    </div>

    </div>
    <div class="sheet-foot">
      <button class="btn" id="btn-cancel" type="button">取消</button>
      <button class="btn primary" id="btn-save" type="button">保存</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
window.onerror = function(m, src, line, col){
  var el = document.getElementById('sub');
  if (el) el.textContent = 'JS 错误: ' + m + ' @' + line + ':' + col;
  return false;
};
const fmt = n => (n === null || n === undefined) ? '—' : n.toLocaleString('zh-CN');
const dlt = n => {
  if (n === null || n === undefined) return '<span class="flat">—</span>';
  if (n > 0) return '<span class="up">+' + fmt(n) + '</span>';
  if (n < 0) return '<span class="down">' + fmt(n) + '</span>';
  return '<span class="flat">0</span>';
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function when(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = n => String(n).padStart(2,'0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 窄屏 / 平板按断点换布局，不用猜屏宽
const MM_NARROW = window.matchMedia('(max-width: 640px)');
const MM_MID = window.matchMedia('(max-width: 900px)');
const defaultCmpMode = () => MM_NARROW.matches ? 'stack' : 'slider';
const pageSize = () => MM_NARROW.matches ? 12 : 25;
const barCount = () => MM_NARROW.matches ? 12 : (MM_MID.matches ? 24 : 40);

let RUNS = [];
let SHOWN = 0;
let OPEN_ID = null;
let OPEN_REQ = new URLSearchParams(location.search).get('open');

function renderBars(){
  const hist = RUNS.slice(0, barCount()).reverse();
  const vals = hist.map(r => ((r.gain||{}).gold) || 0);
  const barsEl = document.getElementById('bars');
  const barsX = document.getElementById('barsx');
  if (hist.length < 2){
    barsEl.innerHTML = '<div class="bars-empty">再跑一轮就能看到趋势</div>';
    barsX.innerHTML = '';
    return;
  }
  const max = Math.max(1, ...vals.map(Math.abs));
  barsEl.innerHTML = hist.map((r,i) => {
    const v = vals[i];
    const h = Math.max(4, Math.abs(v) / max * 76);
    const cls = v < 0 ? 'bar neg' : 'bar';
    return `<div class="${cls}" style="height:${h}px" data-tip="${when(r.start)}  ${fmt(v)}"` +
           ` title="${when(r.start)}  ${fmt(v)}"></div>`;
  }).join('');
  barsX.innerHTML =
    `<span>${when(hist[0].start)}</span><span class="bars-read" id="barsRead"></span>` +
    `<span>${when(hist[hist.length-1].start)}</span>`;
  // 手机上没法 hover，点一下柱子把数值写到中间那块
  barsEl.querySelectorAll('.bar').forEach(b => {
    b.addEventListener('click', () => {
      const out = document.getElementById('barsRead');
      if (out) out.textContent = b.dataset.tip || '';
      barsEl.querySelectorAll('.bar.on').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
    });
  });
}

function rowHTML(r){
  const g = r.gain || {};
  const w = r.workers || [null, null];
  return `<tr data-id="${esc(r.id)}">
    <td class="col-first" data-col="time" title="${esc(r.start)}">${when(r.start)}</td>
    <td class="num hide-sm" data-col="gold" data-label="金币">${dlt(g.gold)}</td>
    <td class="num hide-sm" data-col="elixir" data-label="圣水">${dlt(g.elixir)}</td>
    <td class="num hide-sm" data-col="dark" data-label="暗黑">${dlt(g.dark)}</td>
    <td class="num" data-col="workers" data-label="工人">${(w[0] === null) ? '—' : w[0] + '/' + w[1]}</td>
    <td class="num hide-sm hide-md" data-col="collected" data-label="收集">${r.collected ?? 0}/${r.bubbles ?? 0}</td>
    <td class="num hide-sm hide-md" data-col="dur" data-label="耗时">${(r.dur_s ?? r.duration) ? (r.dur_s ?? r.duration) + 's' : '—'}</td>
    <td data-col="status" data-label="状态">${statusPill(r.status)}</td>
  </tr>`;
}

function renderRows(){
  const body = document.getElementById('rows');
  if (!SHOWN) SHOWN = pageSize();
  const slice = RUNS.slice(0, SHOWN);
  body.innerHTML = slice.map(rowHTML).join('');
  body.querySelectorAll('tr[data-id]').forEach(tr => {
    tr.addEventListener('click', () => toggle(tr, RUNS.find(x => x.id === tr.dataset.id)));
  });
  const more = document.getElementById('moreRows');
  if (more){
    const rest = RUNS.length - slice.length;
    const wrap = document.getElementById('moreWrap');
    if (wrap) wrap.style.display = rest > 0 ? 'block' : 'none';
    more.style.display = rest > 0 ? 'inline-flex' : 'none';
    more.textContent = '加载更多（还有 ' + rest + ' 条）';
  }
  // 分钟级刷新后把用户展开着的那条重新展开
  if (OPEN_ID){
    const tr = [...body.querySelectorAll('tr[data-id]')].find(x => x.dataset.id === OPEN_ID);
    if (tr) toggle(tr, RUNS.find(x => x.id === OPEN_ID));
  }
}

function render(runs){
  RUNS = runs || [];
  const latest = RUNS.find(r => r.status === 'ok') || RUNS.find(r => r.before) || RUNS[0];
  const sub = document.getElementById('sub');
  if (!latest){
    sub.textContent = '还没有记录';
    document.getElementById('none').style.display = 'block';
    document.getElementById('rows').innerHTML = '';
    renderBars();
    return;
  }
  sub.textContent = `最近一轮 ${when(latest.start)} · 服务正常`
    + (latest.backend ? ` · 本轮读数 ${BK_LABEL(latest.backend)}` : '');
  document.getElementById('none').style.display = RUNS.length ? 'none' : 'block';

  const c = document.getElementById('cards');
  const pick = o => (o && o.gold !== null && o.gold !== undefined && o.gold > 0) ? o : null;
  const res = pick(latest.final) || pick(latest.after) || pick(latest.before) || latest.after || {};
  const wk = latest.workers || [null, null];
  // read_workers() 返回的是 (空闲, 总数)：0/5 表示全在忙，不是全空闲
  const free = (wk[0] !== null && wk[1] !== null) ? wk[0] : null;
  const kIcon = (name, label) =>
    `<div class="k">${ic(name, 'cic')}<span>${label}</span></div>`;
  c.innerHTML = `
    <div class="card">${kIcon('gold', '金币')}<div class="v">${fmt(res.gold)}
      <small>${dlt((latest.gain||{}).gold)}</small></div></div>
    <div class="card">${kIcon('elixir', '圣水')}<div class="v">${fmt(res.elixir)}
      <small>${dlt((latest.gain||{}).elixir)}</small></div></div>
    <div class="card">${kIcon('dark', '暗黑重油')}<div class="v">${fmt(res.dark)}
      <small>${dlt((latest.gain||{}).dark)}</small></div></div>
    <div class="card"><div class="k">${SVG.worker}<span>空闲工人</span></div><div class="v">${free === null ? '—' : free}
      <small>/ ${wk[1] ?? '—'}</small></div></div>
    <div class="card"><div class="k">${SVG.star}<span>胜利之星</span></div><div class="v">${latest.star ?? '—'}
      <small>/ 5</small></div></div>`;

  renderBars();
  renderRows();

  // ?open=1 自动展开第一条，?open=<id> 展开指定一条，方便截图和分享定位
  if (OPEN_REQ){
    const req = OPEN_REQ;
    OPEN_REQ = null;
    const rows = [...document.querySelectorAll('#rows tr[data-id]')];
    const tr = (req && req !== '1') ? rows.find(x => x.dataset.id === req) : rows[0];
    if (tr) tr.click();
  }
}

// ---------- 前后对比 ----------
function initCompare(root){
  const after = root.querySelector('.pane.after');
  const div = root.querySelector('.divider');
  const knob = root.querySelector('.knob');
  const before = root.querySelector('img.before');
  const btn = root.parentElement.querySelector('[data-cmp-toggle]');
  if (!after || !div || !before) return;
  let pct = 50, drag = false;
  // 上下两张模式下必须清掉内联 clip-path，否则第二张图还是被裁掉一半
  const stacked = () => root.getAttribute('data-mode') === 'stack';

  function apply(){
    if (stacked()){
      after.style.clipPath = '';
      return;
    }
    after.style.clipPath = 'inset(0 0 0 ' + pct + '%)';
    div.style.left = pct + '%';
    knob.style.left = pct + '%';
  }
  function setMode(m, byUser){
    root.setAttribute('data-mode', m);
    if (byUser) root.dataset.user = '1';
    if (btn) btn.textContent = m === 'stack' ? '滑动对比' : '上下两张';
    apply();
  }
  root._setCmpMode = setMode;
  function fromClientX(cx){
    const r = root.getBoundingClientRect();
    if (!r.width) return;
    pct = Math.max(0, Math.min(100, (cx - r.left) / r.width * 100));
    apply();
  }
  function touchX(e){
    const t = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]);
    return t ? t.clientX : null;
  }

  if (window.PointerEvent){
    root.addEventListener('pointerdown', e => {
      if (stacked() || e.button) return;
      drag = true; fromClientX(e.clientX); e.preventDefault();
    });
    window.addEventListener('pointermove', e => { if (drag) fromClientX(e.clientX); });
    window.addEventListener('pointerup', () => { drag = false; });
    window.addEventListener('pointercancel', () => { drag = false; });
  } else {
    root.addEventListener('mousedown', e => {
      if (stacked()) return;
      drag = true; fromClientX(e.clientX); e.preventDefault();
    });
    window.addEventListener('mousemove', e => { if (drag) fromClientX(e.clientX); });
    window.addEventListener('mouseup', () => { drag = false; });
  }
  // 触摸：显式 touchstart/touchmove，配 .cmp 上的 touch-action:none
  root.addEventListener('touchstart', e => {
    if (stacked()) return;
    const x = touchX(e); if (x === null) return;
    drag = true; fromClientX(x); e.preventDefault();
  }, {passive:false});
  root.addEventListener('touchmove', e => {
    if (!drag || stacked()) return;
    const x = touchX(e); if (x === null) return;
    fromClientX(x); e.preventDefault();
  }, {passive:false});
  root.addEventListener('touchend', () => { drag = false; });
  root.addEventListener('touchcancel', () => { drag = false; });

  if (btn){
    btn.addEventListener('click', e => {
      e.stopPropagation();
      setMode(stacked() ? 'slider' : 'stack', true);
    });
  }
  before.addEventListener('load', apply);
  setMode(root.getAttribute('data-mode') || defaultCmpMode());
  apply();
}

function toggle(tr, run){
  if (!run) return;
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('detail-row')){
    next.remove(); tr.removeAttribute('data-open'); OPEN_ID = null; return;
  }
  const bef = run.before || {};
  const afl = run.final || run.after || {};
  const items = (run.items || []).map(i =>
    `<span class="item">${esc(i.name)} · ${fmt(i.price)}${ic(i.kind)}</span>`).join('');
  const upPaid = (run.upgrade && run.upgrade.paid) ? Object.keys(run.upgrade.paid)[0] : null;
  const up = run.upgrade ? `<div class="items"><span class="item up">${SVG.check}
     已升级 ${esc(run.upgrade.name)} · ${fmt(run.upgrade.price)}${ic(upPaid)}</span></div>` : '';
  const rsc = run.research || {};
  const rres = rsc.result || {};
  const rtxt = rres.ok
    ? `研究中 ${esc(rres.name || '?')} 升到 ${esc(rres.target_level || '?')} 级 · ${fmt(rres.panel_price)} ${rres.panel_currency === 'dark' ? '暗黑' : rres.panel_currency === 'elixir' ? '圣水' : '金币'}`
    : (rsc.busy ? `实验室在忙：${esc(rsc.busy)}`
       : (rsc.picked ? `研究候选 ${esc(rsc.picked.name || '?')} · ${fmt(rsc.picked.price)}`
          : (rsc.skipped === 'research_disabled' ? '' : '实验室：本轮没动')));
  const rs = rtxt ? `<div class="items"><span class="item res">${rtxt}</span></div>` : '';
  const err = run.error ? `<div class="err">${esc(run.error)}</div>` : '';
  const rid = encodeURIComponent(run.id);
  const mode = defaultCmpMode();
  const row = document.createElement('tr');
  row.className = 'detail-row';
  row.innerHTML = `<td colspan="8" class="detail-cell">
    <div class="detail">
      <h3>开始前 / 结束后</h3>
      <div class="cmp" data-mode="${mode}">
        <div class="pane" data-cap="开始前"><img class="before" loading="lazy" src="/runs/${rid}/before.jpg" alt="开始前"></div>
        <div class="pane after" data-cap="结束后"><img loading="lazy" src="/runs/${rid}/after.jpg" alt="结束后"></div>
        <div class="divider"></div><div class="knob" aria-hidden="true">⇔</div>
        <span class="tag l">开始前</span><span class="tag r">结束后</span>
      </div>
      <div class="cmp-bar">
        <div class="cmp-nums">
          <span class="cur">${ic('gold')}${fmt(bef.gold)} → ${fmt(afl.gold)}</span>
          <span class="cur">${ic('elixir')}${fmt(bef.elixir)} → ${fmt(afl.elixir)}</span>
          <span class="cur">${ic('dark')}${fmt(bef.dark)} → ${fmt(afl.dark)}</span>
        </div>
        <button class="btn tiny" type="button" data-cmp-toggle>上下两张</button>
      </div>
      ${items ? '<h3 style="margin-top:16px">可升级项</h3><div class="items">' + items + '</div>' : ''}
      ${up}${rs}${err}
    </div></td>`;
  tr.after(row);
  tr.setAttribute('data-open', '1');
  OPEN_ID = run.id;
  const cmp = row.querySelector('.cmp');
  if (cmp) initCompare(cmp);
}

// 横屏 / 窗口变化：没手动切过的对比图跟着断点走
let RS_TIMER = null;
window.addEventListener('resize', () => {
  if (RS_TIMER) clearTimeout(RS_TIMER);
  RS_TIMER = setTimeout(() => {
    document.querySelectorAll('.cmp').forEach(c => {
      if (!c.dataset.user && c._setCmpMode) c._setCmpMode(defaultCmpMode());
    });
    renderBars();
  }, 200);
});

// ---------- 设置 ----------
let CFG = null;

const $ = id => document.getElementById(id);

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('on'), 1800);
}

function renderTimes(list){
  const box = document.getElementById('times');
  box.innerHTML = list.map((t,i) =>
    `<span class="time-item"><input type="time" value="${t}" data-i="${i}" aria-label="巡检时间 ${i+1}">` +
    `<button data-del="${i}" type="button" title="删除" aria-label="删除这个时间">×</button></span>`
  ).join('') + `<button class="chip add" id="addTime" type="button">＋ 添加</button>`;
  box.querySelectorAll('input[type=time]').forEach(inp => {
    inp.addEventListener('change', () => { CFG.schedule[+inp.dataset.i] = inp.value; });
  });
  box.querySelectorAll('button[data-del]').forEach(b => {
    b.addEventListener('click', () => {
      CFG.schedule.splice(+b.dataset.del, 1);
      renderTimes(CFG.schedule);
    });
  });
  const add = document.getElementById('addTime');
  if (add) add.addEventListener('click', () => {
    if (CFG.schedule.length >= 12) return toast('最多 12 个时段');
    CFG.schedule.push('12:00');
    renderTimes(CFG.schedule);
  });
}

const BK_LABEL = b => b === 'memory' ? '内存后端' : 'OCR 后端';

function setBadge(bk){
  const el = document.getElementById('bkBadge');
  if (el) el.textContent = BK_LABEL(bk);
}

function syncChips(){
  document.querySelectorAll('[data-backend]').forEach(b => {
    b.classList.toggle('on', (CFG.backend || 'ocr') === b.dataset.backend);
  });
  document.querySelectorAll('[data-toggle]').forEach(b => {
    const key = b.dataset.toggle;
    const want = b.dataset.val === '1';
    b.classList.toggle('on', !!CFG[key] === want);
  });
  document.querySelectorAll('[data-kind]').forEach(b => {
    b.classList.toggle('on', (CFG.upgrade_kinds || []).includes(b.dataset.kind));
  });
  document.getElementById('maxPrice').value = CFG.max_price;
  document.getElementById('maxUp').value = CFG.max_upgrades;
  document.getElementById('minFree').value = CFG.min_free_workers;
}

function fillForm(){
  renderTimes([...CFG.schedule]);
  syncChips();
}

async function openCfg(){
  if (window.__BOOT__ && window.__BOOT__.config) {
    CFG = JSON.parse(JSON.stringify(window.__BOOT__.config));
  } else {
    CFG = await (await fetch('/api/config')).json();
  }
  fillForm();
  var m = document.getElementById('mask');
  if (m) m.classList.add('on');
  document.body.classList.add('sheet-open');
  var b = document.querySelector('.sheet-body');
  if (b) b.scrollTop = 0;
}

function closeCfg(){
  var m = document.getElementById('mask');
  if (m) m.classList.remove('on');
  document.body.classList.remove('sheet-open');
}

document.addEventListener('click', e => {
  const t = e.target.closest('[data-toggle]');
  if (t){ CFG[t.dataset.toggle] = t.dataset.val === '1'; syncChips(); return; }
  const bkb = e.target.closest('[data-backend]');
  if (bkb){ CFG.backend = bkb.dataset.backend; syncChips(); return; }
  const k = e.target.closest('[data-kind]');
  if (k){
    const set = new Set(CFG.upgrade_kinds || []);
    set.has(k.dataset.kind) ? set.delete(k.dataset.kind) : set.add(k.dataset.kind);
    CFG.upgrade_kinds = ['gold','elixir','dark'].filter(x => set.has(x));
    syncChips();
  }
});

async function saveCfg(){
  CFG.max_price = Math.max(0, Math.min(50_000_000, +document.getElementById('maxPrice').value || 0));
  CFG.max_upgrades = Math.max(0, Math.min(5, +document.getElementById('maxUp').value || 0));
  CFG.min_free_workers = Math.max(0, Math.min(5, +document.getElementById('minFree').value || 0));
  if (!CFG.schedule.length) return toast('至少要有一个巡检时间');
  const r = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(CFG)
  });
  if (!r.ok) return toast('保存失败');
  const saved = await r.json().catch(() => CFG);
  CFG.backend = saved.backend || CFG.backend;
  setBadge(CFG.backend);
  toast('已保存，下一轮生效（' + BK_LABEL(CFG.backend) + '）');
  closeCfg();
  load();
}

// ---------- 立即运行 ----------
let runTimer = null;

function setRunning(on){
  const live = document.getElementById('live');
  const btn = document.getElementById('btn-run');
  if (live) live.classList.toggle('on', on);
  if (btn){
    btn.disabled = !!on;
    btn.textContent = on ? '运行中…' : '立即运行';
    btn.style.opacity = on ? .6 : 1;
    btn.style.cursor = on ? 'default' : 'pointer';
  }
}

async function pollRun(){
  try{
    const st = await (await fetch('/api/run/status')).json();
    const txt = document.getElementById('liveText');
    const logEl = document.getElementById('liveLog');
    if (st.running){
      if (txt) txt.textContent = `正在运行巡检… ${Math.round(st.elapsed)}s`;
      if (logEl && st.log) logEl.textContent = st.log.slice(-420);
      return;
    }
    clearInterval(runTimer); runTimer = null;
    setRunning(false);
    if (st.rc === 0) toast('巡检完成');
    else if (st.rc !== null) toast('巡检结束（返回码 ' + st.rc + '）');
    load();
  }catch(e){}
}

async function startRun(){
  const r = await fetch('/api/run', {method:'POST'});
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok){
    toast(d.msg || '启动失败');
    return;
  }
  setRunning(true);
  toast('已启动，约需 2 分钟');
  if (runTimer) clearInterval(runTimer);
  runTimer = setInterval(pollRun, 2000);
  pollRun();
}

// ---------------- 图标 ----------------
const ICONS = (window.__BOOT__ && window.__BOOT__.icons) || {};
const CURS = {gold: '金币', elixir: '圣水', dark: '暗黑', gems: '宝石'};
function ic(name, cls){
  const src = ICONS[name];
  return src ? `<img class="${cls || ''}" src="${src}" alt="${CURS[name] || ''}">` : '';
}
// 建建筑图标：先找真素材（static/icons/b_类型.png），没有再退回「按类型着色的圆角块 + 首字母」
function iconFor(type, size){
  const key = 'b_' + String(type || '').split(' ').join('_');
  if (ICONS[key]) return `<span class="upg-ic"><img src="${ICONS[key]}" alt=""></span>`;
  let h = 200;
  for (const ch of String(type || '?')) h = (h * 31 + ch.charCodeAt(0)) % 360;
  const t = String(type || '?');
  // 中文取前两个字（「加农炮」→「加农」），英文取首字母（Cannon → C）
  const initial = /[\u4e00-\u9fff]/.test(t) ? t.slice(0, 2) : t.slice(0, 1).toUpperCase();
  return `<span class="upg-ic fallback" style="--h:${h}" title="${esc(t)}（暂无该建筑的图标）">${esc(initial)}</span>`;
}
// 手绘 SVG（游戏风：圆角 + 深描边），不用 emoji
const SVG = {
  flask: '<svg viewBox="0 0 24 24" fill="none"><path d="M9.2 2.6h5.6M10.4 2.8v6.1L5.9 17.2A2.6 2.6 0 0 0 8.2 21h7.6a2.6 2.6 0 0 0 2.3-3.8L13.6 8.9V2.8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M8.1 15.4h7.9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="10.6" cy="18" r="1.15" fill="currentColor" opacity=".7"/><circle cx="13.7" cy="18.4" r=".85" fill="currentColor" opacity=".55"/></svg>',
  hammer: '<svg viewBox="0 0 24 24"><g transform="rotate(-38 12 12)"><rect x="9.5" y="3.2" width="5" height="8.2" rx="1.3" fill="#dce1e9" stroke="#4a525d" stroke-width="1.5"/><rect x="11" y="10.4" width="2" height="10.4" rx="1" fill="#b9793f" stroke="#7a4a22" stroke-width="1.3"/></g></svg>',
  worker: '<svg viewBox="0 0 24 24" fill="none"><path d="M4.6 20.4c0-3.6 2.9-5.6 7.4-5.6s7.4 2 7.4 5.6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="8.4" r="3.5" fill="currentColor" opacity=".85"/><path d="M7.6 6.2h8.8a1.4 1.4 0 0 0 0-2.8H7.6a1.4 1.4 0 0 0 0 2.8z" fill="currentColor" opacity=".55"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9.2" fill="#16a34a" opacity=".14"/><path d="m7.4 12.4 3.2 3.2 6.2-6.6" stroke="#16a34a" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.6" stroke="#d97706" stroke-width="2"/><path d="M12 7.6V12l3.1 2" stroke="#d97706" stroke-width="2" stroke-linecap="round"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9.2" fill="#dc2626" opacity=".13"/><path d="m8.4 8.4 7.2 7.2M15.6 8.4l-7.2 7.2" stroke="#dc2626" stroke-width="2.3" stroke-linecap="round"/></svg>',
  dot: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.6" fill="currentColor"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="M12 3.2l2.7 5.6 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1L3.2 9.7l6.1-.9z" fill="#f0b03a" stroke="#c2820f" stroke-width="1.1" stroke-linejoin="round"/></svg>',
  brick: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="7" width="18" height="10" rx="2.4" stroke="currentColor" stroke-width="1.7"/><path d="M3 11.6h18M9 7v4.6M15 11.6V17" stroke="currentColor" stroke-width="1.5"/></svg>'
};

// 状态 → 图标 + 极短标注（不写 ok/timeout/failed 这种词）
function statusPill(s){
  if (s === 'ok') return `<span class="st ok" title="完成">${SVG.check}<span>完成</span></span>`;
  if (s === 'failed' || s === 'error') return `<span class="st bad" title="失败">${SVG.x}<span>失败</span></span>`;
  if (s === 'interrupted') return `<span class="st warn" title="中断">${SVG.clock}<span>中断</span></span>`;
  if (s === 'running') return `<span class="st warn" title="运行中">${SVG.clock}<span>运行中</span></span>`;
  return `<span class="st warn">${SVG.dot}<span>${esc(s)}</span></span>`;
}

// ---------------- 正在升级（时间推算 + 本地每秒倒数） ----------------
let ST = null;          // /api/state 最近一份
let ST_SKEW = 0;        // 服务器时间 - 本机时间（毫秒）
const MM_NARROW_ICON = window.matchMedia('(max-width: 640px)');

function pad2(n){ return String(n).padStart(2, '0'); }
function hhmm(ts){
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '—';
  return `${pad2(d.getMonth()+1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function fmtLeft(sec){
  if (sec === null || sec === undefined) return '—';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + '秒';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  if (d) return d + '天' + h + '小时';
  if (h) return h + '小时' + m + '分';
  return m + '分';
}
function num(n){ return (n === null || n === undefined) ? '—' : Number(n).toLocaleString('zh-CN'); }

function serverNow(){ return (Date.now() + ST_SKEW) / 1000; }

function calcItem(it, now){
  const started = it.started_ts, dur = it.duration_s;
  const elapsed = started ? (now - started) : null;
  const remaining = (dur && elapsed !== null) ? Math.max(0, dur - elapsed) : null;
  const prog = (dur && elapsed !== null) ? Math.min(1, Math.max(0, elapsed / dur)) : null;
  return {elapsed, remaining, prog};
}

function upgRow(it){
  const isLab = it.kind === 'research';
  const lv = (it.from_level && it.to_level)
    ? `<span class="upg-lv"><b>${it.from_level}</b><i>→</i><b>${it.to_level}</b></span>` : '';
  const flag = it.estimated ? '<span class="upg-flag" title="开始时间/时长有一个是推算的">时间近似</span>' : '';
  const noDur = it.duration_s ? '' : '<span class="upg-flag" title="这一笔开工时没读到面板时长（老记录），只能看已过时间">时长未知</span>';
  const icon = isLab ? `<span class="upg-ic" style="color:var(--lab)">${SVG.flask}</span>` : iconFor(it.type);
  const cost = it.cost ? `<span class="k">${ic(it.currency)}${num(it.cost)}</span>` : '';
  return `<div class="upg-row ${isLab ? 'research' : ''}" data-k="${esc(it.key)}">
    ${icon}
    <div class="upg-body">
      <div class="upg-l1">
        <span class="upg-name">${esc(isLab ? (it.name || '研究') : it.type)}</span>
        ${lv}${flag}${noDur}
      </div>
      <div class="upg-track-wrap">
        <span class="cap start" title="开工">${SVG.hammer}</span>
        <div class="upg-track">
          <span class="fill"></span><span class="knob"></span>
        </div>
        <span class="cap end" title="完成">${SVG.check}</span>
      </div>
      <div class="upg-l2">
        <span class="pct"></span>
        <span class="k" data-k="elapsed"></span>
        <span class="k" data-k="total"></span>
        ${cost}
        <span class="k" data-k="start"></span>
      </div>
    </div>
    <div class="upg-time"><b></b><span>剩余</span></div>
  </div>`;
}

function chipsHTML(st){
  const b = `<span class="kchip" title="工人在施工">${SVG.hammer}<b>${st.busy}/${st.total}</b> 工人</span>`;
  const lab = st.research
    ? `<span class="kchip" style="color:var(--lab)" title="实验室在研究中">${SVG.flask}<b>研究中</b></span>`
    : `<span class="kchip" title="实验室空闲">${SVG.flask}<b>空闲</b></span>`;
  return b + lab;
}

function renderUpg(){
  const list = document.getElementById('upgList');
  const chips = document.getElementById('upgChips');
  if (!list) return;
  const st = ST;
  if (!st){ return; }
  if (chips) chips.innerHTML = chipsHTML(st);
  const items = st.items || [];
  if (!items.length){
    list.innerHTML = `<div class="upg-empty">${SVG.hammer}<div>工人全空闲 · 没有在建<br>
      <span style="color:var(--ink-3);font-size:12.5px">下一轮巡检有合适的项目就开工</span></div></div>`;
    return;
  }
  list.innerHTML = items.map(upgRow).join('');
  tickUpg();
}

function tickUpg(){
  if (!ST) return;
  const now = serverNow();
  const list = document.getElementById('upgList');
  if (!list) return;
  for (const it of (ST.items || [])){
    const row = list.querySelector(`.upg-row[data-k="${CSS.escape(it.key)}"]`);
    if (!row) continue;
    const {elapsed, remaining, prog} = calcItem(it, now);
    const fill = row.querySelector('.fill');
    const knob = row.querySelector('.knob');
    if (prog === null){
      fill.style.width = '0%';
      fill.style.opacity = '';
      fill.classList.remove('done');
      row.classList.add('nodur');
      knob.style.display = 'none';
    } else {
      row.classList.remove('nodur');
      fill.style.opacity = '';
      fill.style.width = (prog * 100).toFixed(2) + '%';
      knob.style.display = '';
      knob.style.left = (prog * 100).toFixed(2) + '%';
      fill.classList.toggle('done', !!remaining === false || remaining === 0);
    }
    const big = row.querySelector('.upg-time b');
    const small = row.querySelector('.upg-time span');
    const overdue = it.duration_s && remaining === 0;
    row.classList.toggle('done', !!overdue);
    if (overdue){
      big.textContent = '已完成';
      small.textContent = '等下一轮确认';
    } else if (remaining === null){
      big.textContent = fmtLeft(elapsed);
      small.textContent = '已过 · 时长未知';
    } else {
      big.textContent = fmtLeft(remaining);
      small.textContent = '剩余';
    }
    const pct = row.querySelector('.pct');
    if (pct) pct.textContent = prog === null ? '' : Math.round(prog * 100) + '%';
    const set = (k, v) => { const el = row.querySelector(`[data-k="${k}"]`); if (el) el.innerHTML = v; };
    set('elapsed', it.duration_s ? `${SVG.dot}已过 ${fmtLeft(elapsed)}` : '');
    set('total', it.duration_s ? `${SVG.dot}共 ${fmtLeft(it.duration_s)}` : '');
    set('start', it.started_ts ? `${SVG.dot}开始 ${hhmm(it.started_ts)}` : '');
  }
}

async function loadState(){
  try{
    const r = await fetch('/api/state', {cache: 'no-store'});
    if (!r.ok) throw new Error('state ' + r.status);
    const d = await r.json();
    ST = d;
    ST_SKEW = (d.now * 1000) - Date.now();
    renderUpg();
  }catch(e){
    const sub = document.getElementById('sub');
    if (sub && !ST) sub.textContent = '在建状态读取失败：' + e.message;
  }
}

function bootRender(){
  if (window.__BOOT__ && window.__BOOT__.runs) render(window.__BOOT__.runs);
}

async function load(){
  try{
    const r = await fetch('/api/runs');
    render(await r.json());
  }catch(e){
    if (!(window.__BOOT__ && window.__BOOT__.runs))
      document.getElementById('sub').textContent = '读取失败：' + e.message;
  }
}
function bind(id, ev, fn){
  var el = document.getElementById(id);
  if (el) el.addEventListener(ev, fn);
  return el;
}
bind('btn-run', 'click', startRun);
bind('btn-cfg', 'click', openCfg);
bind('btn-x', 'click', closeCfg);
bind('btn-cancel', 'click', closeCfg);
bind('btn-save', 'click', saveCfg);
bind('moreRows', 'click', function(){ SHOWN += pageSize(); renderRows(); });
bind('mask', 'click', function(e){
  if (e.target && e.target.id === 'mask') closeCfg();
});
document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeCfg(); });

if (window.__BOOT__ && window.__BOOT__.config) setBadge(window.__BOOT__.config.backend);
if (window.__BOOT__ && window.__BOOT__.state){
  ST = window.__BOOT__.state;
  ST_SKEW = (ST.now * 1000) - Date.now();
  renderUpg();
}
bootRender();
load();
loadState();
setInterval(load, 60000);
setInterval(loadState, 30000);      // 只刷「在建」这一块，不整页刷新
setInterval(tickUpg, 1000);         // 倒计时本地每秒走，不打扰后端

// 页面刷新后如果后端还在跑，自动接上进度
fetch('/api/run/status').then(r => r.json()).then(st => {
  if (st.running){
    setRunning(true);
    if (!runTimer) runTimer = setInterval(pollRun, 2000);
    pollRun();
  }
}).catch(() => {});

if (new URLSearchParams(location.search).get('cfg')) {
  openCfg();
}
</script>
</body>
</html>
"""


def load_runs():
    out = []
    if not os.path.isdir(HIST):
        return out
    for name in os.listdir(HIST):
        p = os.path.join(HIST, name, "meta.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            d.setdefault("id", name)
            if d.get("status") == "running":
                # 进程被杀时 meta 不会收尾，超过 15 分钟就认为中断
                age = time.time() - os.path.getmtime(p)
                if age > 900:
                    d["status"] = "interrupted"
                    d["error"] = "巡检进程中断（未正常收尾）"
            out.append(d)
        except Exception:
            continue
    out.sort(key=lambda d: d.get("start") or d.get("id") or "", reverse=True)
    return out[:MAX_RUNS]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/run":
            p = _proc.get("p")
            if p is not None and p.poll() is None:
                return self._send(409, json.dumps(
                    {"ok": False, "msg": "已有巡检在跑"}, ensure_ascii=False))
            try:
                logf = open(RUN_LOG, "w")
                logf.write("")
                logf.close()
                f = open(RUN_LOG, "a")
                proc = subprocess.Popen([PYTHON, "-u", RUN_SCRIPT],
                                        stdout=f, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                _proc["p"] = proc
                _proc["started"] = time.time()
                return self._send(200, json.dumps({"ok": True, "msg": "已启动"}, ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
        if path != "/api/config":
            return self._send(404, b"", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(n).decode())
            saved = coc_config.save(data)
        except Exception as e:
            return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False))
        return self._send(200, json.dumps(saved, ensure_ascii=False))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            cfg = coc_config.load()
            boot = json.dumps({"runs": load_runs(), "config": cfg,
                               "icons": icon_map(), "state": state_payload()},
                              ensure_ascii=False).replace("</", "<\\/")
            # 后端徽标服务端先渲染一次，页面还没跑 JS 时也能看出当前是哪个后端
            html = PAGE.replace("__BACKEND_LABEL__",
                                BACKEND_LABELS.get(cfg.get("backend", "ocr"), "OCR 后端"), 1)
            # 必须注入在主脚本之前，否则 bootRender/openCfg 读 window.__BOOT__ 还是 undefined
            html = html.replace("<script>\nwindow.onerror",
                                "<script>window.__BOOT__=" + boot + ";</script>\n<script>\nwindow.onerror", 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode())))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Coc-Backend", cfg.get("backend", "ocr"))
            self.end_headers()
            self.wfile.write(html.encode())
            return
        if path == "/api/runs":
            return self._send(200, json.dumps(load_runs(), ensure_ascii=False))
        if path == "/api/state":
            try:
                return self._send(200, json.dumps(state_payload(), ensure_ascii=False))
            except Exception as e:
                return self._send(500, json.dumps(
                    {"error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False))
        if path == "/api/config":
            return self._send(200, json.dumps(coc_config.load(), ensure_ascii=False))
        if path == "/api/run/status":
            p = _proc.get("p")
            running = p is not None and p.poll() is None
            elapsed = round(time.time() - _proc["started"], 1) if running and _proc.get("started") else 0
            tail = ""
            try:
                with open(RUN_LOG, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 3000))
                    tail = f.read().decode("utf-8", "replace")
            except Exception:
                pass
            rc = None if running else (p.returncode if p is not None else None)
            return self._send(200, json.dumps(
                {"running": running, "elapsed": elapsed, "rc": rc, "log": tail},
                ensure_ascii=False))
        m = re.fullmatch(r"/runs/([0-9A-Za-z_\-]+)/(before|after)\.jpg", path)
        if m:
            rid, which = m.groups()
            if not re.fullmatch(r"[0-9A-Za-z_\-]+", rid):
                return self._send(404, b"")
            fp = os.path.join(HIST, rid, which + ".jpg")
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(404, b"")
        return self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    os.makedirs(HIST, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"coc dashboard on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
