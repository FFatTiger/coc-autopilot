#!/usr/bin/env python3
"""部落冲突巡检看板：读 coc_history/ 下的 meta.json，出状态页 + 历史 + 前后截图。

单文件、零依赖（标准库 http.server），供 PM2 常驻。
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coc_config
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HIST = os.environ.get("COC_HISTORY") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history")
PORT = 8899
MAX_RUNS = 200

RUN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coc_patrol_run.py")
PYTHON = os.environ.get("COC_PYTHON") or sys.executable
RUN_LOG = "/tmp/coc_manual_run.log"
_proc = {"p": None, "started": None}

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>部落冲突 · 巡检看板</title>
<style>
  :root{
    --bg:#f2f3f5; --card:#fff; --ink:#1b1d21; --ink-2:#6b7280; --ink-3:#9ca3af;
    --line:#eceef1; --gold:#e8a11c; --elixir:#b04ce0; --dark:#4b4257;
    --ok:#16a34a; --bad:#dc2626; --warn:#d97706;
    --r:16px; --r-sm:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:20px;font-weight:600;margin:0 0 4px}
  .sub{color:var(--ink-2);font-size:13px;margin-bottom:22px}
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
  .bar{flex:1;min-width:6px;border-radius:4px 4px 0 0;background:var(--gold);opacity:.85;
       transition:opacity .15s}
  .bar:hover{opacity:1}
  .bar.neg{background:var(--ink-3)}
  .bars-x{display:flex;justify-content:space-between;color:var(--ink-3);font-size:11px;margin-top:6px}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;color:var(--ink-2);font-weight:500;font-size:12px;
     padding:0 12px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:13px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
  tbody tr{cursor:pointer}
  tbody tr:hover td{background:#fafbfc}
  td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
        background:#f3f4f6;color:var(--ink-2);border:1px solid var(--line)}
  .pill.ok{background:#f0fdf4;color:var(--ok);border-color:#dcfce7}
  .pill.bad{background:#fef2f2;color:var(--bad);border-color:#fee2e2}
  .pill.warn{background:#fffbeb;color:var(--warn);border-color:#fef3c7}
  .up{color:var(--ok)} .down{color:var(--bad)} .flat{color:var(--ink-3)}
  .detail{background:#fafbfc;border-radius:var(--r-sm);padding:16px;margin:2px 0 12px}
  .detail h3{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--ink-2)}
  .shots{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .cmp{position:relative;overflow:hidden;border-radius:var(--r-sm);border:1px solid var(--line);
       background:#fff;user-select:none;touch-action:none}
  .cmp img{display:block;width:100%;pointer-events:none}
  .cmp .after{position:absolute;inset:0;overflow:hidden}
  .cmp .after img{position:absolute;top:0;left:0;height:100%;width:auto;min-width:100%}
  .cmp .divider{position:absolute;top:0;bottom:0;width:2px;background:#fff;
       box-shadow:0 0 0 1px rgba(0,0,0,.25);pointer-events:none}
  .cmp .knob{position:absolute;top:50%;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;
       background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.22);display:flex;align-items:center;
       justify-content:center;font-size:13px;color:var(--ink-2);pointer-events:none}
  .cmp .tag{position:absolute;top:8px;padding:3px 9px;border-radius:999px;font-size:11px;
       background:rgba(17,19,23,.62);color:#fff;pointer-events:none}
  .cmp .tag.l{left:8px} .cmp .tag.r{right:8px}
  .cmp-bar{display:flex;align-items:center;gap:10px;margin-top:10px;font-size:12px;color:var(--ink-3)}
  .shot{background:#fff;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden}
  .shot img{width:100%;display:block}
  .shot .cap{padding:8px 12px;font-size:12px;color:var(--ink-2);border-top:1px solid var(--line)}
  .items{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .item{background:#fff;border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:12.5px}
  .empty{color:var(--ink-3);padding:28px 0;text-align:center;font-size:14px}
  .btn{border:1px solid var(--line);background:#fff;color:var(--ink);border-radius:10px;
        padding:8px 16px;font-size:14px;cursor:pointer;font-family:inherit}
  .btn:hover{background:#fafbfc}
  .btn.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
  .btn.primary:hover{opacity:.88}
  .mask{position:fixed;inset:0;background:rgba(17,19,23,.32);display:none;z-index:50;
         align-items:flex-start;justify-content:center;padding:6vh 16px;overflow:auto}
  .mask.on{display:flex}
  .sheet{background:#fff;border-radius:20px;width:100%;max-width:520px;padding:24px 26px 20px;
          border:1px solid var(--line)}
  .sheet h2{font-size:17px;font-weight:600;margin:0 0 4px}
  .sheet .hint{color:var(--ink-2);font-size:13px;margin-bottom:18px}
  .field{margin-bottom:18px}
  .field label{display:block;font-size:13px;color:var(--ink-2);margin-bottom:7px}
  .field .desc{font-size:12px;color:var(--ink-3);margin-top:6px;line-height:1.5}
  .chips{display:flex;flex-wrap:wrap;gap:8px}
  .chip{border:1px solid var(--line);background:#fff;color:var(--ink-2);border-radius:999px;
         padding:6px 13px;font-size:13px;cursor:pointer;font-family:inherit}
  .chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
  .chip.add{border-style:dashed;color:var(--ink-3)}
  input[type=number],input[type=time]{font:inherit;border:1px solid var(--line);border-radius:10px;
        padding:8px 12px;width:100%;background:#fff;color:var(--ink)}
  .times{display:flex;flex-wrap:wrap;gap:8px}
  .time-item{display:flex;align-items:center;gap:6px;background:#f7f8f9;border-radius:10px;padding:4px 4px 4px 10px}
  .time-item input{border:0;background:transparent;width:96px;padding:4px 0;font-variant-numeric:tabular-nums}
  .time-item button{border:0;background:transparent;color:var(--ink-3);cursor:pointer;font-size:16px;
        line-height:1;padding:4px 8px;border-radius:6px;font-family:inherit}
  .time-item button:hover{background:#eceef1;color:var(--ink)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .sheet-foot{display:flex;justify-content:flex-end;gap:10px;margin-top:22px;padding-top:18px;
        border-top:1px solid var(--line)}
  .live{border:1px solid #dcfce7;background:#f0fdf4;border-radius:auto;padding:0}
  .live{display:none;margin-top:14px;border:1px solid #dcfce7;background:#f0fdf4;border-radius:var(--r-sm);
        padding:12px 16px;font-size:13px;color:#166534}
  .live.on{display:block}
  .live .row{display:flex;align-items:center;gap:10px}
  .spinner{width:13px;height:13px;border:2px solid #bbf7d0;border-top-color:#16a34a;border-radius:50%;
        animation:spin .8s linear infinite;flex:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .live pre{margin:8px 0 0;max-height:110px;overflow:auto;font-size:11.5px;line-height:1.5;
        color:#14532d;background:#fff;border-radius:8px;padding:8px 10px;white-space:pre-wrap}
  .toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:var(--ink);color:#fff;
         padding:10px 18px;border-radius:999px;font-size:14px;opacity:0;transition:opacity .2s;z-index:60;
         pointer-events:none}
  .toast.on{opacity:1}
  @media (max-width:720px){
    .shots{grid-template-columns:1fr}
    .card .v{font-size:22px}
    td.hide-sm,th.hide-sm{display:none}
  }
</style>
</head>
<body>
<div class="wrap">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px">
    <div>
      <h1>部落冲突 · 巡检看板</h1>
      <div class="sub" id="sub">加载中…</div>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn primary" id="btn-run">立即运行</button>
      <button class="btn" id="btn-cfg">设置</button>
    </div>
  </div>

  <div class="grid cards" id="cards"></div>

  <div class="section">
    <h2>最近每轮金币收益</h2>
    <div class="card">
      <div class="bars" id="bars"></div>
      <div class="bars-x" id="barsx"></div>
    </div>
  </div>

  <div class="section">
    <h2>历史记录</h2>
    <div class="card" style="padding:18px 8px 4px">
      <table>
        <thead><tr>
          <th style="padding-left:20px">时间</th>
          <th class="hide-sm">金币</th><th class="hide-sm">圣水</th><th class="hide-sm">暗黑</th>
          <th>工人</th><th class="hide-sm">收集</th><th class="hide-sm">耗时</th><th>状态</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="none" style="display:none">还没有巡检记录，等第一轮跑完就有了。</div>
    </div>
  </div>
</div>

<div class="live" id="live">
  <div class="row"><span class="spinner"></span><span id="liveText">正在运行巡检…</span></div>
  <pre id="liveLog"></pre>
</div>

<div class="mask" id="mask">
  <div class="sheet">
    <h2>设置</h2>
    <div class="hint">按北京时间。保存后下一轮巡检即生效，不需要重启服务。</div>

    <div class="field">
      <label>巡检时间</label>
      <div class="times" id="times"></div>
      <div class="desc">每天在这些时刻跑一轮。</div>
    </div>

    <div class="field">
      <label>收资源</label>
      <div class="chips">
        <button class="chip" data-toggle="collect" data-val="1">开启</button>
        <button class="chip" data-toggle="collect" data-val="0">关闭</button>
      </div>
    </div>

    <div class="field">
      <label>自动升级</label>
      <div class="chips">
        <button class="chip" data-toggle="upgrade" data-val="1">开启</button>
        <button class="chip" data-toggle="upgrade" data-val="0">关闭</button>
      </div>
      <div class="desc">关闭后只收集和巡检，不花任何资源。</div>
    </div>

    <div class="field">
      <label>允许花费的资源</label>
      <div class="chips" id="kinds">
        <button class="chip" data-kind="gold">金币</button>
        <button class="chip" data-kind="elixir">圣水</button>
        <button class="chip" data-kind="dark">暗黑重油</button>
      </div>
      <div class="desc">暗黑重油比较稀有，默认关闭。</div>
    </div>

    <div class="row2">
      <div class="field">
        <label>单次升级价格上限</label>
        <input type="number" id="maxPrice" min="0" max="50000000" step="100000">
        <div class="desc">超过这个价格的项目会跳过。</div>
      </div>
      <div class="field">
        <label>每轮最多升几个</label>
        <input type="number" id="maxUp" min="0" max="5" step="1">
        <div class="desc">0 表示不升级。</div>
      </div>
    </div>

    <div class="field">
      <label>至少保留的空闲工人</label>
      <input type="number" id="minFree" min="0" max="5" step="1">
      <div class="desc">留 1 表示始终给手动操作留一个工人；填 0 表示有多少用多少。</div>
    </div>

    <div class="sheet-foot">
      <button class="btn" id="btn-cancel">取消</button>
      <button class="btn primary" id="btn-save">保存</button>
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

function render(runs){
  const latest = runs.find(r => r.status === 'ok') || runs.find(r => r.before) || runs[0];
  const sub = document.getElementById('sub');
  if (!latest){
    sub.textContent = '还没有记录';
    document.getElementById('none').style.display = 'block';
    return;
  }
  sub.textContent = `最近一轮 ${when(latest.start)} · 服务正常`;
  document.getElementById('none').style.display = runs.length ? 'none' : 'block';

  const c = document.getElementById('cards');
  const pick = o => (o && o.gold !== null && o.gold !== undefined && o.gold > 0) ? o : null;
  const res = pick(latest.final) || pick(latest.after) || pick(latest.before) || latest.after || {};
  const wk = latest.workers || [null, null];
  const free = (wk[0] !== null && wk[1] !== null) ? wk[1] - wk[0] : null;
  c.innerHTML = `
    <div class="card"><div class="k"><i class="dot gold"></i>金币</div><div class="v">${fmt(res.gold)}
      <small>${dlt((latest.gain||{}).gold)}</small></div></div>
    <div class="card"><div class="k"><i class="dot elixir"></i>圣水</div><div class="v">${fmt(res.elixir)}
      <small>${dlt((latest.gain||{}).elixir)}</small></div></div>
    <div class="card"><div class="k"><i class="dot dark"></i>暗黑重油</div><div class="v">${fmt(res.dark)}
      <small>${dlt((latest.gain||{}).dark)}</small></div></div>
    <div class="card"><div class="k">空闲工人</div><div class="v">${free === null ? '—' : free}
      <small>/ ${wk[1] ?? '—'}</small></div></div>
    <div class="card"><div class="k">胜利之星</div><div class="v">${latest.star ?? '—'}
      <small>/ 5</small></div></div>`;

  const hist = runs.slice(0, 40).reverse();
  const vals = hist.map(r => ((r.gain||{}).gold) || 0);
  const barsEl = document.getElementById('bars');
  const barsX = document.getElementById('barsx');
  if (hist.length < 2){
    barsEl.innerHTML = '<div style="flex:1;text-align:center;color:var(--ink-3);font-size:13px;padding:26px 0">' +
      '再跑一轮就能看到趋势</div>';
    barsX.innerHTML = '';
  } else {
    const max = Math.max(1, ...vals.map(Math.abs));
    barsEl.innerHTML = hist.map((r,i) => {
      const v = vals[i];
      const h = Math.max(4, Math.abs(v) / max * 76);
      const cls = v < 0 ? 'bar neg' : 'bar';
      return `<div class="${cls}" style="height:${h}px" title="${when(r.start)}  ${fmt(v)}"></div>`;
    }).join('');
    barsX.innerHTML =
      `<span>${when(hist[0].start)}</span><span>${when(hist[hist.length-1].start)}</span>`;
  }

  document.getElementById('rows').innerHTML = runs.map(r => {
    const g = r.gain || {};
    const w = r.workers || [null, null];
    const free = (w[0] !== null && w[1] !== null) ? w[1] - w[0] : null;
    const st = r.status === 'ok' ? '<span class="pill ok">完成</span>'
             : r.status === 'failed' ? '<span class="pill bad">失败</span>'
             : r.status === 'error' ? '<span class="pill bad">异常</span>'
             : r.status === 'interrupted' ? '<span class="pill warn">中断</span>'
             : r.status === 'running' ? '<span class="pill warn">运行中</span>'
             : '<span class="pill warn">' + esc(r.status) + '</span>';
    return `<tr data-id="${esc(r.id)}">
      <td style="padding-left:20px" title="${esc(r.start)}">${when(r.start)}</td>
      <td class="num hide-sm">${dlt(g.gold)}</td>
      <td class="num hide-sm">${dlt(g.elixir)}</td>
      <td class="num hide-sm">${dlt(g.dark)}</td>
      <td class="num">${(w[0] === null) ? '—' : w[0] + '/' + w[1]}</td>
      <td class="num hide-sm">${r.collected ?? 0}/${r.bubbles ?? 0}</td>
      <td class="num hide-sm">${r.duration ? r.duration + 's' : '—'}</td>
      <td>${st}</td>
    </tr>`;
  }).join('');

  document.querySelectorAll('#rows tr').forEach(tr => {
    tr.addEventListener('click', () => toggle(tr, runs.find(x => x.id === tr.dataset.id)));
  });

  // ?open=1 自动展开第一条，方便截图和分享定位
  if (new URLSearchParams(location.search).get('open')) {
    const first = document.querySelector('#rows tr');
    if (first) first.click();
  }
}

function initCompare(root){
  const bar = root.querySelector('.after');
  const div = root.querySelector('.divider');
  const knob = root.querySelector('.knob');
  const before = root.querySelector('img.before');
  if (!bar || !div || !before) return;
  let pct = 50;

  function apply(){
    bar.style.clipPath = `inset(0 0 0 ${pct}%)`;
    div.style.left = pct + '%';
    knob.style.left = pct + '%';
  }
  function fromEvent(e){
    const r = root.getBoundingClientRect();
    const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
    pct = Math.max(0, Math.min(100, x / r.width * 100));
    apply();
  }
  let drag = false;
  root.addEventListener('pointerdown', e => { drag = true; fromEvent(e); });
  window.addEventListener('pointermove', e => { if (drag) fromEvent(e); });
  window.addEventListener('pointerup', () => { drag = false; });
  before.addEventListener('load', apply);
  apply();
}

function toggle(tr, run){
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('detail-row')){
    next.remove(); return;
  }
  const items = (run.items || []).map(i =>
    `<span class="item">${esc(i.name)} · ${fmt(i.price)} ${i.kind === 'gold' ? '金币' : i.kind === 'elixir' ? '圣水' : i.kind === 'dark' ? '暗黑' : '未知'}</span>`).join('');
  const up = run.upgrade ? `<div class="items"><span class="item" style="border-color:#dcfce7;color:#16a34a">
     已升级 ${esc(run.upgrade.name)} · ${fmt(run.upgrade.price)}</span></div>` : '';
  const err = run.error ? `<div style="color:#dc2626;font-size:13px;margin-top:10px">${esc(run.error)}</div>` : '';
  const row = document.createElement('tr');
  row.className = 'detail-row';
  row.innerHTML = `<td colspan="8" style="padding:0 12px 14px">
    <div class="detail">
      <h3>开始前 / 结束后</h3>
      <div class="cmp" data-id="${esc(run.id)}">
        <img class="before" loading="lazy" src="/runs/${encodeURIComponent(run.id)}/before.jpg" alt="开始前">
        <div class="after"><img loading="lazy" src="/runs/${encodeURIComponent(run.id)}/after.jpg" alt="结束后"></div>
        <div class="divider"></div><div class="knob">⇔</div>
        <span class="tag l">开始前</span><span class="tag r">结束后</span>
      </div>
      <div class="cmp-bar">
        <span>金币 ${fmt((run.before||{}).gold)} → ${fmt((run.final||run.after||{}).gold)}</span>
        <span>·</span>
        <span>圣水 ${fmt((run.before||{}).elixir)} → ${fmt((run.final||run.after||{}).elixir)}</span>
        <span style="margin-left:auto">拖动分割线对比</span>
      </div>
      ${items ? '<h3 style="margin-top:16px">可升级项</h3><div class="items">' + items + '</div>' : ''}
      ${up}${err}
    </div></td>`;
  tr.after(row);
  const cmp = row.querySelector('.cmp');
  if (cmp) initCompare(cmp);
}

// ---------- 设置 ----------
let CFG = null;

const $ = id => document.getElementById(id);

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('on');
  setTimeout(() => t.classList.remove('on'), 1800);
}

function renderTimes(list){
  const box = document.getElementById('times');
  box.innerHTML = list.map((t,i) =>
    `<span class="time-item"><input type="time" value="${t}" data-i="${i}"><button data-del="${i}" title="删除">×</button></span>`
  ).join('') + `<button class="chip add" id="addTime">＋ 添加</button>`;
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

function syncChips(){
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
}

function closeCfg(){ var m = document.getElementById('mask'); if (m) m.classList.remove('on'); }

document.addEventListener('click', e => {
  const t = e.target.closest('[data-toggle]');
  if (t){ CFG[t.dataset.toggle] = t.dataset.val === '1'; syncChips(); return; }
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
  toast('已保存，下一轮生效');
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
bind('btn-cancel', 'click', closeCfg);
bind('btn-save', 'click', saveCfg);
bind('mask', 'click', function(e){
  if (e.target && e.target.id === 'mask') closeCfg();
});
document.addEventListener('keydown', function(e){ if (e.key === 'Escape') closeCfg(); });

bootRender();
load();
setInterval(load, 60000);

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
            boot = json.dumps({"runs": load_runs(), "config": coc_config.load()},
                              ensure_ascii=False).replace("</", "<\\/")
            # 必须注入在主脚本之前，否则 bootRender/openCfg 读 window.__BOOT__ 还是 undefined
            html = PAGE.replace("<script>\nwindow.onerror",
                                "<script>window.__BOOT__=" + boot + ";</script>\n<script>\nwindow.onerror", 1)
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/runs":
            return self._send(200, json.dumps(load_runs(), ensure_ascii=False))
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
