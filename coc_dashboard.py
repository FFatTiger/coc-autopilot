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
import coc_names
import coc_progress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HIST = os.environ.get("COC_HISTORY") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history")
PORT = int(os.environ.get("COC_PORT") or 8899)   # 同一台机跑第二份看板（比如试新后端）时改这个
MAX_RUNS = 200
BACKEND_LABELS = {"ocr": "截图识别", "memory": "内存后端"}

ROOT = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(ROOT, "static", "icons")      # tools/icons_fetch.py 生成

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
    """「正在升级」这一块的数据：状态文件里那部分 + 服务器当前时间。

    展示字段在这里就换成中文（coc_names），前端只管画——键名和数值不动。
    """
    cfg = coc_config.load()
    raw = coc_progress.build(total_builders=cfg.get("builders", 5),
                             build_max_h=cfg.get("build_max_h", 168),
                             research_max_h=cfg.get("research_max_h", 240))
    return coc_names.localize_state(raw)


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
  /* ================= 配色：沿用游戏那套（金 / 圣水紫 / 暗黑灰紫 / 宝石绿） ================= */
  :root{
    color-scheme:light dark;
    --bg:#f3f1ee;
    --card:#fff; --surface:#fff; --surface-2:#f8f7f5; --surface-3:#f1efec;
    --ink:#241d2e; --ink-2:#6e6579; --ink-3:#a49cae;
    --line:#ece8ef; --line-2:#e4dfe8;
    --gold:#f5c542; --gold-2:#ffdd7a; --gold-deep:#d9a319;
    --elixir:#d05ce3; --elixir-2:#e98af6; --elixir-deep:#a83fbb;
    --dark:#6b4f8a; --dark-2:#8f6fb0; --dark-deep:#4b3568;
    --gems:#4caf50; --gems-2:#7bd97f; --gems-deep:#2f8f34;
    --ok:#3aa757; --bad:#e05252; --warn:#e08a1e;
    --ok-bg:#eefaf0; --ok-bd:#d6f0dc;
    --bad-bg:#fdeeee; --bad-bd:#f8d8d8;
    --warn-bg:#fff7e8; --warn-bd:#f7e6c4;
    --chip-bg:#f2eff5;
    --info:#7a5cc4; --info-bg:#f2edff; --info-bd:#e3d8ff;
    --accent:#2b2237; --on-accent:#fff;
    --lab:#8b5cf6; --lab-bg:#f6f1ff; --lab-bd:#e8dcff; --lab-bar:#a78bfa; --lab-2:#c9b6ff;
    --live-bg:#eefaf0; --live-bd:#d6f0dc; --live-ink:#2c7a44; --live-pre:#fff; --live-spin:#cdeed6;
    --track:#eeeaf2; --track-2:#e2dcea;
    --toast-bg:#241d2e; --toast-ink:#fff;
    --shadow-sm:0 1px 2px rgba(36,29,46,.05);
    --shadow:0 1px 2px rgba(36,29,46,.04), 0 10px 26px -14px rgba(36,29,46,.22);
    --shadow-lift:0 2px 6px rgba(36,29,46,.06), 0 18px 38px -18px rgba(36,29,46,.26);
    --r:20px; --r-sm:14px; --r-xs:10px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#131017;
      --card:#1c1824; --surface:#1c1824; --surface-2:#231e2d; --surface-3:#2a2436;
      --ink:#efeaf6; --ink-2:#a79fb5; --ink-3:#7d7490;
      --line:#2a2434; --line-2:#332c3f;
      --gold:#f7ce5c; --gold-2:#ffe293; --gold-deep:#e6bb3a;
      --elixir:#dc7cea; --elixir-2:#f0a8fa; --elixir-deep:#c05cd0;
      --dark:#a58bc6; --dark-2:#c3aede; --dark-deep:#8a6cae;
      --gems:#68cf6d; --gems-2:#96e69a; --gems-deep:#4bb552;
      --ok:#5cd07a; --bad:#f4776f; --warn:#f3b13f;
      --ok-bg:#16281c; --ok-bd:#21402a;
      --bad-bg:#2b1719; --bad-bd:#432225;
      --warn-bg:#2b2213; --warn-bd:#41331b;
      --chip-bg:#272133;
      --info:#b7a0f5; --info-bg:#221c33; --info-bd:#332a4d;
      --accent:#efeaf6; --on-accent:#191521;
      --lab:#b79aff; --lab-bg:#221b33; --lab-bd:#352a4e; --lab-bar:#a78bfa; --lab-2:#c9b6ff;
      --live-bg:#16281c; --live-bd:#21402a; --live-ink:#8fe3a5; --live-pre:#101a13; --live-spin:#26452f;
      --track:#2a2436; --track-2:#352d44;
      --toast-bg:#efeaf6; --toast-ink:#191521;
      --shadow-sm:0 1px 2px rgba(0,0,0,.3);
      --shadow:0 1px 2px rgba(0,0,0,.35), 0 14px 30px -18px rgba(0,0,0,.7);
      --shadow-lift:0 2px 8px rgba(0,0,0,.45), 0 22px 44px -22px rgba(0,0,0,.85);
    }
  }
  *,*::before,*::after{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
            "Microsoft YaHei",system-ui,sans-serif;
       -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:rgba(0,0,0,.06)}
  body.sheet-open{overflow:hidden}
  img{max-width:100%}
  .wrap{max-width:1120px;margin:0 auto;padding:30px 22px 72px}

  /* ---------------- 顶部 ---------------- */
  .topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;
       margin-bottom:26px}
  h1{font-size:21px;font-weight:700;letter-spacing:-.2px;margin:0 0 6px;
     display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .sub{color:var(--ink-2);font-size:13px}
  .topbar .actions{display:flex;gap:9px;flex:none}
  .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:600;
        background:var(--chip-bg);color:var(--ink-2);border:1px solid transparent}
  .pill.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-bd)}
  .pill.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--bad-bd)}
  .pill.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-bd)}

  /* ---------------- 资源卡 ---------------- */
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fit,minmax(196px,1fr))}
  .card{background:var(--card);border-radius:var(--r);border:1px solid var(--line);
        box-shadow:var(--shadow);padding:18px 20px 17px;position:relative;overflow:hidden}
  .card .k{color:var(--ink-2);font-size:12.5px;font-weight:500;margin-bottom:10px;
       display:flex;align-items:center;gap:8px;letter-spacing:.2px}
  .card .k img.cic{width:20px;height:20px;display:block}
  .card .k svg{width:16px;height:16px;display:block;opacity:.7}
  .card .v{font-size:27px;font-weight:700;letter-spacing:-.8px;line-height:1.1;
       font-variant-numeric:tabular-nums}
  .card .v small{font-size:13px;color:var(--ink-3);font-weight:500;margin-left:5px;letter-spacing:0}

  /* 资源卡：图标带一圈淡淡的本色光、数字更突出 */
  .res{isolation:isolate}
  .res::after{content:"";position:absolute;z-index:-1;top:-54px;right:-46px;width:124px;height:124px;
       border-radius:50%;background:radial-gradient(circle,var(--tint) 0%,transparent 72%);
       opacity:.34;pointer-events:none}
  .res .k{margin-bottom:12px}
  .res .k img.cic{width:27px;height:27px;filter:drop-shadow(0 2px 3px rgba(0,0,0,.14))}
  .res .v{font-size:30px}
  .res .delta{margin-top:9px;font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums;
       display:inline-flex;align-items:center;gap:4px;padding:2px 9px 2px 7px;border-radius:999px;
       background:var(--chip-bg);color:var(--ink-2)}
  .res .delta.up{background:var(--ok-bg);color:var(--ok)}
  .res .delta.down{background:var(--bad-bg);color:var(--bad)}
  .res .delta.flat{background:var(--chip-bg);color:var(--ink-3)}
  .res.gold{--tint:var(--gold)} .res.elixir{--tint:var(--elixir)}
  .res.dark{--tint:var(--dark)} .res.plain{--tint:var(--ink-3)}

  /* ---------------- 区块标题 ---------------- */
  .section{margin-top:30px}
  .section h2{font-size:14.5px;font-weight:700;margin:0 0 13px;color:var(--ink);
       display:flex;align-items:center;gap:8px;letter-spacing:.2px}
  .section h2::before{content:"";width:4px;height:15px;border-radius:2px;
       background:linear-gradient(180deg,var(--gold),var(--elixir));display:inline-block}

  /* ---------------- 收益柱状 ---------------- */
  .bars-wrap{background:var(--card);border-radius:var(--r);border:1px solid var(--line);
       box-shadow:var(--shadow);padding:18px 20px 14px}
  .bars{display:flex;align-items:flex-end;gap:5px;height:96px;padding:6px 0 0;
       border-bottom:1px solid var(--line);position:relative}
  .bars::before{content:"";position:absolute;left:0;right:0;top:33%;border-top:1px dashed var(--line);
       pointer-events:none}
  .bar{flex:1 1 0;min-width:5px;border-radius:6px 6px 2px 2px;
       background:linear-gradient(180deg,var(--gold-2),var(--gold));
       opacity:.9;transition:opacity .15s,transform .12s;cursor:pointer}
  .bar:hover{transform:translateY(-1px)}
  .bar.neg{background:linear-gradient(180deg,var(--bad),color-mix(in srgb,var(--bad) 55%,transparent))}
  .bar.zero{background:var(--track-2);opacity:.55}
  .bar.on{opacity:1;box-shadow:0 0 0 2px color-mix(in srgb,var(--gold) 35%,transparent)}
  .bars-empty{flex:1;text-align:center;color:var(--ink-3);font-size:13px;padding:30px 0}
  .bars-x{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
          color:var(--ink-3);font-size:11px;margin-top:9px}
  .bars-note{color:var(--ink-3);font-size:11px;margin-top:6px;text-align:right}

  .pips{display:flex;gap:5px;margin-top:10px}
  .pips i{width:13px;height:13px;border-radius:4px;display:block}
  .pips i.on{background:linear-gradient(180deg,var(--gold-2),var(--gold));
        box-shadow:inset 0 1px 0 rgba(255,255,255,.5)}
  .pips i.off{background:var(--track);box-shadow:inset 0 0 0 1.5px var(--line-2)}
  .bars-read{flex:1 1 auto;text-align:center;color:var(--ink-2);font-size:inherit;
             font-variant-numeric:tabular-nums;min-width:0;overflow:hidden;
             text-overflow:ellipsis;white-space:nowrap}

  /* ---------------- 历史表 ---------------- */
  .hist-card{background:var(--card);border-radius:var(--r);border:1px solid var(--line);
       box-shadow:var(--shadow);padding:6px 6px 2px;overflow:hidden}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;color:var(--ink-3);font-weight:600;font-size:11.5px;letter-spacing:.4px;
     padding:12px 12px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:12px;border-bottom:1px solid var(--line);vertical-align:middle}
  tbody tr:last-child td{border-bottom:0}
  th.col-first,td.col-first{padding-left:18px}
  tbody tr[data-id]{cursor:pointer;transition:background .12s}
  td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .up{color:var(--ok);font-weight:600} .down{color:var(--bad);font-weight:600}
  .flat{color:var(--ink-3)}
  .cur{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
  .cur img{width:16px;height:16px;display:inline-block}
  .st{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;
      color:var(--ink-2);white-space:nowrap;padding:3px 9px 3px 6px;border-radius:999px;
      background:var(--chip-bg)}
  .st svg{width:14px;height:14px;display:block}
  .st.ok{color:var(--ok);background:var(--ok-bg)}
  .st.bad{color:var(--bad);background:var(--bad-bg)}
  .st.warn{color:var(--warn);background:var(--warn-bg)}

  /* ---------------- 详情 ---------------- */
  .detail{background:var(--surface-2);border-radius:var(--r-sm);padding:18px;margin:4px 0 14px}
  .detail h3{margin:0 0 12px;font-size:12.5px;font-weight:700;color:var(--ink-2);letter-spacing:.3px}
  .detail-cell{padding:0 10px 14px}
  .items{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .item{background:var(--surface);border:1px solid var(--line);border-radius:999px;
        padding:5px 13px;font-size:12.5px;display:inline-flex;align-items:center;gap:5px}
  .item.up{border-color:var(--ok-bd);background:var(--ok-bg);color:var(--ok)}
  .item.res{border-color:var(--info-bd);background:var(--info-bg);color:var(--info)}
  .item img{width:15px;height:15px;display:inline-block}
  .item svg{width:15px;height:15px;display:block}
  .err{color:var(--bad);font-size:13px;margin-top:10px}
  .empty{color:var(--ink-3);padding:30px 0;text-align:center;font-size:14px}
  .more-wrap{display:none;padding:10px 12px 14px}

  /* 前后对比 */
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
       box-shadow:0 0 0 1px rgba(0,0,0,.22);pointer-events:none;z-index:2}
  .cmp .knob{position:absolute;top:50%;left:50%;width:38px;height:38px;margin:-19px 0 0 -19px;
       border-radius:50%;background:#fff;box-shadow:0 3px 12px rgba(0,0,0,.24);
       display:flex;align-items:center;justify-content:center;font-size:14px;color:#41474f;
       pointer-events:none;z-index:3}
  .cmp .tag{position:absolute;top:8px;padding:3px 10px;border-radius:999px;font-size:11px;
       background:rgba(24,20,30,.6);color:#fff;pointer-events:none;z-index:2;
       backdrop-filter:blur(4px)}
  .cmp .tag.l{left:8px} .cmp .tag.r{right:8px}
  .cmp[data-mode="stack"]{display:grid;grid-template-columns:1fr;gap:10px;border:0;
       background:transparent;overflow:visible;touch-action:auto;cursor:default;border-radius:0}
  .cmp[data-mode="stack"] .pane{position:relative;inset:auto;overflow:hidden;clip-path:none;
       border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface)}
  .cmp[data-mode="stack"] .pane.after img{position:static;width:100%;height:auto}
  .cmp[data-mode="stack"] .divider,
  .cmp[data-mode="stack"] .knob,
  .cmp[data-mode="stack"] .tag{display:none}
  .cmp[data-mode="stack"] .pane::after{content:attr(data-cap);display:block;padding:8px 12px;
       font-size:12px;color:var(--ink-2);border-top:1px solid var(--line);background:var(--surface-2)}
  .cmp-bar{display:flex;align-items:center;gap:12px;margin-top:12px;font-size:12px;
           color:var(--ink-3);flex-wrap:wrap}
  .cmp-nums{display:flex;flex-wrap:wrap;gap:4px 16px;flex:1 1 220px;min-width:0;
            font-variant-numeric:tabular-nums;color:var(--ink-2)}

  /* ---------------- 按钮 / 抽屉 ---------------- */
  .btn{border:1px solid var(--line-2);background:var(--surface);color:var(--ink);border-radius:12px;
        padding:9px 17px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;
        display:inline-flex;align-items:center;justify-content:center;gap:6px;
        box-shadow:var(--shadow-sm);transition:background .12s,transform .06s,box-shadow .12s}
  /* 主按钮走游戏里的金色：深色字 + 一点点高光 */
  .btn.primary{background:linear-gradient(180deg,var(--gold-2),var(--gold));color:#3d2a04;
        border-color:transparent;font-weight:700;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.55), 0 6px 16px -10px rgba(217,163,25,.9)}
  .btn.tiny{padding:7px 13px;font-size:12.5px;border-radius:10px;min-height:34px;box-shadow:none}
  .btn:active{background:var(--surface-2);transform:translateY(1px)}
  .btn.primary:active{opacity:.9}
  .btn:disabled{cursor:default}
  .mask{position:fixed;inset:0;background:rgba(24,18,32,.42);backdrop-filter:blur(2px);
        display:none;z-index:50;align-items:flex-start;justify-content:center;
        padding:6vh 16px;overflow:auto}
  .mask.on{display:flex}
  .sheet{background:var(--surface);border-radius:22px;width:100%;max-width:520px;
          padding:26px 28px 22px;border:1px solid var(--line);display:flex;flex-direction:column;
          box-shadow:var(--shadow-lift);max-height:88vh;max-height:88dvh}
  .sheet-head{flex:none}
  .sheet-body{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain}
  .sheet h2{font-size:17px;font-weight:700;margin:0 0 5px}
  .sheet .hint{color:var(--ink-2);font-size:13px;margin-bottom:18px}
  .sheet-x{display:none}
  .field{margin-bottom:18px}
  .field label{display:block;font-size:13px;font-weight:600;color:var(--ink-2);margin-bottom:8px}
  .field .desc{font-size:12px;color:var(--ink-3);margin-top:7px;line-height:1.6}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{border:1px solid var(--line-2);background:var(--surface);color:var(--ink-2);border-radius:999px;
         padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit;
         display:inline-flex;align-items:center;justify-content:center;
         transition:background .12s,color .12s,border-color .12s}
  .chip.on{background:var(--accent);color:var(--on-accent);border-color:transparent}
  .chip.add{border-style:dashed;color:var(--ink-3)}
  .chip:active{background:var(--surface-2)}
  .chip.on:active{opacity:.9}
  input[type=number],input[type=text]{font:inherit;border:1px solid var(--line-2);
        border-radius:12px;padding:9px 13px;width:100%;background:var(--surface);color:var(--ink)}
  .times{display:flex;flex-wrap:wrap;gap:8px}
  .time-item{display:flex;align-items:center;gap:6px;background:var(--surface-3);border-radius:12px;
        padding:4px 4px 4px 11px}
  .time-item input{border:0;background:transparent;width:92px;padding:4px 0;
        font-variant-numeric:tabular-nums}
  .time-item button{border:0;background:transparent;color:var(--ink-3);cursor:pointer;font-size:16px;
        line-height:1;padding:5px 9px;border-radius:8px;font-family:inherit}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .sheet-foot{display:flex;justify-content:flex-end;gap:10px;margin-top:22px;padding-top:18px;
        border-top:1px solid var(--line)}
  .live{display:none;margin-top:16px;border:1px solid var(--live-bd);background:var(--live-bg);
        border-radius:var(--r-sm);padding:13px 16px;font-size:13px;color:var(--live-ink)}
  .live.on{display:block}
  .live .row{display:flex;align-items:center;gap:10px}
  .spinner{width:14px;height:14px;border:2px solid var(--live-spin);border-top-color:var(--ok);
        border-radius:50%;animation:spin .8s linear infinite;flex:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .live pre{margin:9px 0 0;max-height:120px;overflow:auto;font-size:11.5px;line-height:1.5;
        color:var(--live-ink);background:var(--live-pre);border-radius:10px;padding:9px 11px;
        white-space:pre-wrap;word-break:break-all}
  .toast{position:fixed;left:50%;bottom:34px;transform:translateX(-50%);background:var(--toast-bg);
         color:var(--toast-ink);padding:11px 20px;border-radius:999px;font-size:14px;opacity:0;
         transition:opacity .2s;z-index:60;pointer-events:none;max-width:calc(100% - 32px);
         text-align:center;box-shadow:var(--shadow-lift)}
  .toast.on{opacity:1}

  /* ---------------- 正在升级：一张一张的小卡片 ---------------- */
  .upg{margin-top:30px}
  .upg-head{display:flex;align-items:center;justify-content:space-between;gap:12px;
       margin:0 0 13px;flex-wrap:wrap}
  .upg-head h2{margin:0}
  .upg-chips{display:flex;gap:8px;flex-wrap:wrap}
  .kchip{display:inline-flex;align-items:center;gap:6px;padding:5px 12px 5px 9px;border-radius:999px;
       background:var(--card);border:1px solid var(--line);color:var(--ink-2);font-size:12px;
       line-height:1.5;font-variant-numeric:tabular-nums;white-space:nowrap;
       box-shadow:var(--shadow-sm)}
  .kchip b{color:var(--ink);font-weight:700}
  .kchip svg{width:14px;height:14px;display:block;opacity:.9}
  .upg-list{display:grid;gap:11px}

  .upg-row{background:var(--card);border:1px solid var(--line);border-radius:18px;
       padding:14px 16px 13px;box-shadow:var(--shadow);position:relative;overflow:hidden;
       transition:box-shadow .15s,transform .12s}
  .upg-main{display:grid;grid-template-columns:48px minmax(0,1fr) auto;align-items:center;gap:14px}
  .upg-ic{width:48px;height:48px;border-radius:14px;overflow:hidden;flex:none;position:relative;
       background:radial-gradient(circle at 50% 38%,var(--surface-3),var(--surface-2));
       display:grid;place-items:center;box-shadow:inset 0 0 0 1px var(--line)}
  .upg-ic img{width:100%;height:100%;object-fit:contain;display:block;padding:2px}
  .upg-ic svg{width:24px;height:24px;display:block}
  /* 兜底图标：按类型取色 + 简化几何形，不摆首字母 */
  .upg-ic.fallback{--h:40;background:
       radial-gradient(circle at 50% 30%, hsl(var(--h) 58% 68%), hsl(var(--h) 52% 46%));
       box-shadow:inset 0 1px 0 rgba(255,255,255,.35), inset 0 -8px 14px rgba(0,0,0,.16)}
  .upg-ic.fallback svg{width:26px;height:26px;color:#fff;opacity:.95;
       filter:drop-shadow(0 1px 1px rgba(0,0,0,.28))}
  .upg-body{min-width:0}
  .upg-l1{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:5px}
  .upg-name{font-weight:700;font-size:15px;letter-spacing:-.1px;white-space:nowrap;
       overflow:hidden;text-overflow:ellipsis;max-width:100%}
  .upg-lv{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:600;
       color:var(--ink-2);font-variant-numeric:tabular-nums;background:var(--chip-bg);
       border-radius:999px;padding:2px 9px}
  .upg-lv b{color:var(--ink);font-weight:700}
  .upg-lv i{color:var(--ink-3);font-style:normal;font-size:10px}
  .upg-flag{font-size:11px;font-weight:600;color:var(--warn);background:var(--warn-bg);
       border-radius:999px;padding:2px 9px;line-height:1.5}
  .upg-l2{display:flex;align-items:center;gap:6px 12px;flex-wrap:wrap;font-size:12px;
       color:var(--ink-2);font-variant-numeric:tabular-nums}
  .upg-l2 .k{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
  .upg-l2 img{width:14px;height:14px;display:block}
  .upg-l2 .pct{color:var(--ink);font-weight:700}
  .upg-time{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;
       padding-left:6px}
  .upg-time b{display:block;font-size:19px;font-weight:700;letter-spacing:-.4px;line-height:1.2}
  .upg-time span{font-size:11px;color:var(--ink-3)}

  /* 进度条：通栏贴底，渐变、无边框 */
  .upg-track-wrap{margin-top:12px}
  .upg-track{position:relative;flex:1 1 auto;height:10px;border-radius:999px;
       background:var(--track);overflow:visible}
  .upg-track .fill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;
       background:linear-gradient(90deg,var(--gold-deep),var(--gold) 55%,var(--gold-2));
       box-shadow:0 1px 6px -1px color-mix(in srgb,var(--gold) 60%,transparent);
       transition:width .8s cubic-bezier(.4,0,.2,1)}
  .upg-row.research .upg-track .fill{
       background:linear-gradient(90deg,var(--lab),var(--lab-bar) 55%,var(--lab-2));
       box-shadow:0 1px 6px -1px color-mix(in srgb,var(--lab) 45%,transparent)}
  .upg-track .fill.done{background:linear-gradient(90deg,var(--ok),var(--gems-2));
       box-shadow:0 1px 6px -1px color-mix(in srgb,var(--ok) 50%,transparent)}
  .upg-track .knob{position:absolute;top:50%;width:16px;height:16px;border-radius:50%;
       background:var(--card);border:2.5px solid var(--gold);transform:translate(-50%,-50%);
       box-shadow:0 2px 6px rgba(0,0,0,.2);transition:left .8s cubic-bezier(.4,0,.2,1);z-index:2}
  .upg-row.research .upg-track .knob{border-color:var(--lab-bar)}
  /* 时长未知：虚线轨道，不装作知道进度 */
  .upg-row.nodur .upg-track{background-image:repeating-linear-gradient(115deg,
       var(--track-2) 0 7px, transparent 7px 14px);background-color:var(--track)}
  .upg-row.nodur .upg-time b{color:var(--ink-2);font-weight:600;font-size:17px}

  .upg-row.research{background:linear-gradient(180deg,var(--lab-bg),var(--card) 70%);
       border-color:var(--lab-bd)}
  .upg-row.done{border-color:var(--ok-bd)}
  .upg-row.done .upg-time b{color:var(--ok)}
  .upg-empty{display:flex;align-items:center;gap:13px;color:var(--ink-2);font-size:13.5px;
       background:var(--card);border:1px dashed var(--line-2);border-radius:18px;padding:18px 20px}
  .upg-empty svg{width:24px;height:24px;opacity:.45;flex:none}

  @media (hover:hover){
    .bar:hover{opacity:1}
    tbody tr[data-id]:hover td{background:var(--surface-2)}
    .upg-row:hover{box-shadow:var(--shadow-lift);transform:translateY(-1px)}
    .btn:hover{background:var(--surface-2)}
    .btn.primary:hover{background:linear-gradient(180deg,#ffe18c,var(--gold));
         box-shadow:inset 0 1px 0 rgba(255,255,255,.6), 0 8px 20px -10px rgba(217,163,25,.95)}
    .chip:hover{background:var(--surface-2)}
    .chip.on:hover{background:var(--accent);opacity:.92}
    .time-item button:hover{background:var(--line);color:var(--ink)}
  }
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }

  /* ---- 平板 ---- */
  @media (max-width:900px){
    .wrap{padding:24px 18px 60px}
    .cards{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
    .card .v{font-size:24px}
    .res .v{font-size:26px}
    .row2{grid-template-columns:1fr}
    td.hide-md,th.hide-md{display:none}
    .btn{min-height:44px}
    .chip{min-height:44px;padding:0 16px}
    .time-item{min-height:46px;padding:2px 2px 2px 12px}
    .time-item button{width:44px;height:44px;display:flex;align-items:center;justify-content:center}
    input[type=number],input[type=text]{font-size:16px;min-height:46px}
  }

  /* ---- 手机 ---- */
  @media (max-width:640px){
    .wrap{padding:16px 14px calc(38px + env(safe-area-inset-bottom))}
    .topbar{flex-direction:column;align-items:stretch;gap:13px;margin-bottom:18px}
    .topbar .actions{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .btn{min-height:44px;padding:11px 16px;font-size:15px;width:100%}
    .btn.tiny{min-height:40px;font-size:13px;width:auto}
    h1{font-size:18px;line-height:1.4}
    .sub{font-size:12.5px}
    .cards{grid-template-columns:1fr 1fr;gap:10px}
    .card{padding:13px 14px 12px;border-radius:16px}
    .card .k{font-size:12px;margin-bottom:7px}
    .card .k img.cic{width:22px;height:22px}
    .card .v{font-size:20px;letter-spacing:-.4px}
    .res .v{font-size:21px}
    .res .delta{font-size:11.5px;margin-top:7px}
    .cards > .card:last-child:nth-child(odd){grid-column:1/-1}
    .section{margin-top:22px}
    .section h2{margin-bottom:10px;font-size:14px}
    .bars-wrap{padding:14px 14px 12px;border-radius:16px}
    .bars{height:82px;gap:3px;padding:6px 0 0;overflow:hidden}
    .bar{min-width:0}
    .bars-x{font-size:10.5px}
    .live{padding:11px 13px}
    .hist-card{padding:0;background:transparent;border:0;border-radius:0;box-shadow:none}
    table,tbody{display:block;width:100%}
    table thead{display:none}
    tbody tr[data-id]{display:grid;grid-template-columns:1fr 1fr;align-items:baseline;
         gap:5px 14px;border:1px solid var(--line);border-radius:14px;background:var(--card);
         padding:12px 14px;margin:0 0 9px;box-shadow:var(--shadow-sm)}
    tbody tr[data-id]:active{background:var(--surface-2)}
    tbody tr[data-id]:last-child{border-bottom:1px solid var(--line)}
    tbody tr.detail-row{display:block;border:0;background:transparent;padding:0;
         margin:-5px 0 11px;border-radius:0;box-shadow:none}
    tbody tr.detail-row td{display:block;padding:0;border:0}
    td{display:inline-flex;align-items:baseline;gap:5px;
       padding:0;border:0;font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
    td::before{content:attr(data-label);color:var(--ink-3);font-size:11.5px;font-weight:400;flex:none}
    td.col-first{padding-left:0}
    td.hide-sm,td.hide-md{display:inline-flex}
    th.hide-sm,th.hide-md{display:none}
    td[data-col="time"]{order:1;grid-column:1;font-weight:700;font-size:14px;
         justify-content:flex-start;gap:6px;padding-bottom:2px}
    td[data-col="time"]::before{content:none}
    td[data-col="time"]::after{content:"›";color:var(--ink-3);font-size:16px;line-height:1;
         transition:transform .15s}
    tr[data-open] td[data-col="time"]::after{transform:rotate(90deg)}
    td[data-col="status"]{order:2;grid-column:2;justify-self:end;padding-bottom:2px}
    td[data-col="status"]::before{content:none}
    td[data-col="gold"]{order:3} td[data-col="elixir"]{order:4}
    td[data-col="dark"]{order:5} td[data-col="workers"]{order:6}
    td[data-col="collected"]{order:7} td[data-col="dur"]{order:8;color:var(--ink-3)}
    td.num{white-space:nowrap;font-size:13px}
    .more-wrap{padding:4px 0 12px}
    .more-wrap .btn{width:100%}
    .detail{padding:13px;border-radius:14px}
    .cmp-bar{flex-direction:column;align-items:stretch;gap:9px}
    .cmp-nums{flex:0 1 auto;flex-direction:column;gap:3px;font-size:12.5px}
    .item{font-size:12px;padding:5px 11px}
    /* 在建卡片：图标 + 名字一行，倒计时挪到右下 */
    .upg{margin-top:22px}
    .upg-head{margin-bottom:10px;gap:8px}
    .upg-chips{gap:6px}
    .kchip{font-size:11.5px;padding:4px 10px 4px 8px}
    .upg-row{padding:12px 13px 12px;border-radius:16px}
    .upg-main{grid-template-columns:42px minmax(0,1fr) auto;gap:11px;align-items:start}
    .upg-ic{width:42px;height:42px;border-radius:12px}
    .upg-l1{margin-bottom:4px;gap:6px}
    .upg-name{font-size:14.5px}
    .upg-lv{font-size:11px;padding:1px 8px}
    .upg-l2{font-size:11.5px;gap:3px 9px}
    .upg-time b{font-size:15px}
    .upg-time span{font-size:10.5px}
    .upg-track-wrap{margin-top:10px;gap:8px}
    .upg-empty{padding:15px;font-size:13px;border-radius:16px}
    .mask{align-items:flex-end;justify-content:stretch;padding:0}
    .sheet{max-width:none;width:100%;border-radius:22px 22px 0 0;border-bottom:0;
           padding:18px 16px 0;max-height:92vh;max-height:92dvh;display:flex;flex-direction:column}
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
    input[type=number],input[type=text]{font-size:16px;min-height:46px;padding:9px 12px}
    .times{gap:8px}
    .time-item{flex:1 1 140px;min-height:46px;padding:2px 2px 2px 12px}
    .time-item input{width:auto;flex:1 1 auto;min-width:0;font-size:16px}
    .time-item button{width:44px;height:44px;font-size:20px;flex:none;display:flex;
           align-items:center;justify-content:center}
    .sheet-foot{flex:none;position:sticky;bottom:0;background:var(--surface);margin:0 -16px;
           padding:12px 16px calc(12px + env(safe-area-inset-bottom));
           border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .sheet-foot .btn{min-height:46px;font-size:15px}
    .toast{bottom:calc(26px + env(safe-area-inset-bottom));font-size:13.5px}
  }
  @media (max-width:380px){
    .card .v{font-size:18px}
    .res .v{font-size:19px}
    .wrap{padding-left:11px;padding-right:11px}
    td{font-size:13px}
    .upg-main{grid-template-columns:38px minmax(0,1fr) auto}
    .upg-ic{width:38px;height:38px}
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
    <div class="bars-wrap">
      <div class="bars" id="bars"></div>
      <div class="bars-x" id="barsx"></div>
      <div class="bars-note"></div>
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
        <button class="chip" data-backend="ocr" type="button">截图识别</button>
        <button class="chip" data-backend="memory" type="button">内存直读</button>
      </div>
      <div class="desc">截图识别兼容性好、不用取权限，但数字会认错位、增量只能靠多轮投票估；
        内存直读逐位精确、地址失效能自愈，但要手机先取最高权限（root）并定位一次地址。
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
      <div class="desc">锁完会回读系统状态，确认屏幕真的灭了；下一轮自己唤醒解锁。</div>
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
  // 只画金币有变化的轮次：跑空轮（游戏没开）会是一排 0，画出来只是脏
  const hist = RUNS.filter(r => ((r.gain || {}).gold)).slice(0, barCount()).reverse();
  const vals = hist.map(r => ((r.gain||{}).gold) || 0);
  const barsEl = document.getElementById('bars');
  const barsX = document.getElementById('barsx');
  if (hist.length < 2){
    barsEl.innerHTML = '<div class="bars-empty">还没有两轮有收益的记录，攒够就画出来</div>';
    barsX.innerHTML = '';
    return;
  }
  const max = Math.max(1, ...vals.map(Math.abs));
  const avg = Math.round(vals.filter(v => v > 0).reduce((a, b) => a + b, 0) /
                         Math.max(1, vals.filter(v => v > 0).length));
  barsEl.innerHTML = hist.map((r,i) => {
    const v = vals[i];
    const h = v === 0 ? 3 : Math.max(6, Math.abs(v) / max * 76);
    const cls = v < 0 ? 'bar neg' : (v === 0 ? 'bar zero' : 'bar');
    return `<div class="${cls}" style="height:${h}px" data-tip="${when(r.start)}  ${fmt(v)}"` +
           ` title="${when(r.start)}  ${fmt(v)}"></div>`;
  }).join('');
  const note = document.querySelector('.bars-note');
  if (note) note.textContent = `共 ${hist.length} 轮有金币变化 · 平均 +${fmt(avg)} · 最高 +${fmt(max)}`;
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
    <td class="num hide-sm hide-md" data-col="dur" data-label="耗时">${(r.dur_s ?? r.duration) ? (r.dur_s ?? r.duration) + ' 秒' : '—'}</td>
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
  // 工人格：亮的是在干活，虚的是空闲（一眼看出几个工人空着）
  const workerPips = (total, freeN) => {
    if (!total) return '<div class="delta flat">读数中没有工人</div>';
    const busyN = (freeN === null || freeN === undefined) ? null : total - freeN;
    let out = '';
    for (let i = 0; i < total; i++){
      const on = busyN === null ? false : i < busyN;
      out += `<i class="${on ? 'on' : 'off'}"></i>`;
    }
    return out;
  };
  // 增量做成小胶囊：涨了绿、掉了红、没动灰
  const gain = latest.gain || {};
  const pill = n => (n === null || n === undefined)
    ? '<div class="delta flat">本轮无数据</div>'
    : `<div class="delta ${n > 0 ? 'up' : n < 0 ? 'down' : 'flat'}">` +
      (n > 0 ? '▲ +' + fmt(n) : n < 0 ? '▼ ' + fmt(n) : '0') + '</div>';
  c.innerHTML = `
    <div class="card res gold">${kIcon('gold', '金币')}
      <div class="v">${fmt(res.gold)}</div>${pill(gain.gold)}</div>
    <div class="card res elixir">${kIcon('elixir', '圣水')}
      <div class="v">${fmt(res.elixir)}</div>${pill(gain.elixir)}</div>
    <div class="card res dark">${kIcon('dark', '暗黑重油')}
      <div class="v">${fmt(res.dark)}</div>${pill(gain.dark)}</div>
    <div class="card res plain"><div class="k">${SVG.worker}<span>空闲工人</span></div>
      <div class="v">${free === null ? '—' : free}<small>/ ${wk[1] ?? '—'}</small></div>
      <div class="pips">${workerPips(wk[1], free)}</div></div>
    <div class="card res plain"><div class="k">${SVG.star}<span>胜利之星</span></div>
      <div class="v">${latest.star ?? '—'}<small>/ 5</small></div>
      <div class="delta flat">本轮战果</div></div>`;

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

// 9:00 / 9：5 / 0900 → 09:00 / 09:05 / 09:00；认不出返回空
function normTime(v){
  const m = String(v || '').trim().replace(/[：.]/g, ':').match(/^(\\d{1,2}):?(\\d{1,2})$/);
  if (!m) return '';
  const h = +m[1], mi = +m[2];
  if (h > 23 || mi > 59) return '';
  return String(h).padStart(2, '0') + ':' + String(mi).padStart(2, '0');
}

function renderTimes(list){
  const box = document.getElementById('times');
  box.innerHTML = list.map((t,i) =>
    `<span class="time-item"><input type="text" value="${t}" data-i="${i}" inputmode="numeric"` +
    ` maxlength="5" pattern="[0-9]{1,2}:[0-9]{2}" placeholder="09:00" aria-label="巡检时间 ${i+1}">` +
    `<button data-del="${i}" type="button" title="删除" aria-label="删除这个时间">×</button></span>`
  ).join('') + `<button class="chip add" id="addTime" type="button">＋ 添加</button>`;
  box.querySelectorAll('.time-item input').forEach(inp => {
    inp.addEventListener('change', () => {
      const v = normTime(inp.value);
      if (!v){ inp.value = CFG.schedule[+inp.dataset.i]; toast('时间要写成 09:00 这样'); return; }
      inp.value = v;
      CFG.schedule[+inp.dataset.i] = v;
    });
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

const BK_LABEL = b => b === 'memory' ? '内存后端' : '截图识别';

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
// 建筑/兵种图标：优先用真素材（static/icons/b_*.png，后端按类型给出 key）；
// 素材缺了就画一枚按类型取色的塔楼剪影，不摆首字母
function iconFor(label, iconKey){
  const key = iconKey && ICONS[iconKey] ? iconKey
            : ('b_' + String(label || '').split(' ').join('_'));
  if (ICONS[key]) return `<span class="upg-ic"><img src="${ICONS[key]}" alt="${esc(label || '')}"></span>`;
  let h = 200;
  for (const ch of String(label || '?')) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `<span class="upg-ic fallback" style="--h:${h}" title="${esc(label || '')}">${SVG.turret}</span>`;
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
  brick: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="7" width="18" height="10" rx="2.4" stroke="currentColor" stroke-width="1.7"/><path d="M3 11.6h18M9 7v4.6M15 11.6V17" stroke="currentColor" stroke-width="1.5"/></svg>',
  // 兜底图标：城垛 + 塔身 + 炮口，比首字母像那么回事
  turret: '<svg viewBox="0 0 24 24" fill="none"><path d="M6.5 9.6V8.1h2.4V6.4h2.3v1.7h2.6V6.4H16v3.2" fill="currentColor" opacity=".92"/><path d="M7.6 9.6h8.8l1.1 9.6a1.4 1.4 0 0 1-1.4 1.5H7.9a1.4 1.4 0 0 1-1.4-1.5z" fill="currentColor" opacity=".72"/><path d="M10.4 12.6h3.2M10.4 15.4h3.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".8"/></svg>'
};

// 状态 → 图标 + 中文标注（后端给的是 ok/timeout 这种键，页面上不露英文）
const ST_ZH = {ok:'正常', done:'已完成', success:'正常', timeout:'超时', failed:'失败',
               error:'出错', interrupted:'中断', running:'运行中', expired:'状态失效',
               partial:'部分完成', skipped:'已跳过'};
function statusPill(s){
  const word = ST_ZH[s] || '未知状态';
  const icon = (s === 'ok' || s === 'done' || s === 'success') ? SVG.check
             : (s === 'failed' || s === 'error') ? SVG.x
             : SVG.clock;
  const cls = (s === 'ok' || s === 'done' || s === 'success') ? 'ok'
            : (s === 'failed' || s === 'error') ? 'bad' : 'warn';
  return `<span class="st ${cls}">${icon}<span>${word}</span></span>`;
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
// 大号倒计时：一天以上用「1天2小时」，一天以内走钟表式秒级（18:59:12），
// 这样每次截图都能看出它在走
function fmtTick(sec){
  if (sec === null || sec === undefined) return '—';
  sec = Math.max(0, Math.round(sec));
  if (sec >= 86400){
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
    return d + '天' + h + '小时';
  }
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
  const p2 = n => String(n).padStart(2, '0');
  return h ? `${h}:${p2(m)}:${p2(s2)}` : `${m}:${p2(s2)}`;
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

// 一条在建 = 一张小卡片：图标在左、信息在右、进度条贴底
function upgRow(it){
  const isLab = it.kind === 'research';
  const title = isLab ? (it.name || '研究') : (it.type || '建筑');
  const lv = (it.from_level && it.to_level)
    ? `<span class="upg-lv"><b>${it.from_level}</b><i>▶</i><b>${it.to_level}</b></span>` : '';
  const flag = it.estimated ? '<span class="upg-flag" title="开始时间或时长是推算的">时间近似</span>' : '';
  const noDur = it.duration_s ? ''
    : '<span class="upg-flag" title="这一笔开工时没读到面板时长，只能看已过时间">时长未知</span>';
  const icon = iconFor(title, it.icon);
  const cost = it.cost ? `<span class="k">${ic(it.currency)}${num(it.cost)}</span>` : '';
  const cur = it.currency ? ` cur-${it.currency}` : '';
  return `<div class="upg-row${isLab ? ' research' : ''}${cur}" data-k="${esc(it.key)}">
    <div class="upg-main">
      ${icon}
      <div class="upg-body">
        <div class="upg-l1">
          <span class="upg-name">${esc(title)}</span>${lv}${flag}${noDur}
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
    </div>
    <div class="upg-track-wrap"><div class="upg-track">` +
      `<span class="fill"></span><span class="knob"></span></div></div>
  </div>`;
}

function chipsHTML(st){
  const b = `<span class="kchip" title="正在施工的工人数">${SVG.hammer}` +
    `工人在忙 <b>${st.busy}/${st.total}</b></span>`;
  const lab = st.research
    ? `<span class="kchip" style="color:var(--lab)" title="实验室正在研究">${SVG.flask}` +
      `实验室 <b>研究中</b></span>`
    : `<span class="kchip" title="实验室没有研究">${SVG.flask}实验室 <b>空闲</b></span>`;
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
      big.textContent = fmtTick(elapsed);
      small.textContent = '已过 · 时长未知';
    } else {
      big.textContent = fmtTick(remaining);
      small.textContent = '剩余';
    }
    const pct = row.querySelector('.pct');
    if (pct) pct.textContent = prog === null ? '' : Math.round(prog * 100) + '%';
    const set = (k, v) => { const el = row.querySelector(`[data-k="${k}"]`); if (el) el.innerHTML = v; };
    set('elapsed', it.duration_s ? `已过 ${fmtLeft(elapsed)}` : '');
    set('total', it.duration_s ? `共 ${fmtLeft(it.duration_s)}` : '');
    set('start', it.started_ts ? `开始 ${hhmm(it.started_ts)}` : '');
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
            coc_names.localize_run(d)
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
