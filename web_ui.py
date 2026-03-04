#!/usr/bin/env python3
"""
web_ui.py — Browser-based interface for the traffic analyser.

Usage:
    python web_ui.py                  # starts on port 5000
    python web_ui.py --port 5001      # custom port

Access at: http://192.168.1.99:5000
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, date

from flask import (Flask, Response, jsonify, render_template_string,
                   request, send_file, stream_with_context)

from config import RECORDINGS_ROOT
import database as db

app = Flask(__name__)

# ── State ─────────────────────────────────────────────────────────────────────

# Currently running job (only one at a time)
_job_lock   = threading.Lock()
_job        = {
    "running":   False,
    "pid":       None,
    "log":       [],
    "type":      None,   # "analyse" or "batch"
    "started":   None,
    "output":    None,   # path to annotated video if produced
}

ANNOTATED_DIR = "/volume1/traffic/annotated"
os.makedirs(ANNOTATED_DIR, exist_ok=True)

VENV_PYTHON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../traffic_venv/bin/python"
)
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

ANALYSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyse.py")
BATCHER  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch.py")


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Traffic Analyser</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;600;800&display=swap');

  :root {
    --bg:      #0a0c0f;
    --surface: #111418;
    --border:  #1e2530;
    --accent:  #00e5ff;
    --accent2: #ff6b35;
    --text:    #c8d0dc;
    --muted:   #4a5568;
    --green:   #00ff88;
    --red:     #ff4444;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Syne', sans-serif;
    min-height: 100vh;
    display: grid;
    grid-template-rows: auto 1fr;
  }

  header {
    border-bottom: 1px solid var(--border);
    padding: 1.2rem 2rem;
    display: flex;
    align-items: center;
    gap: 1.5rem;
    background: var(--surface);
  }

  header h1 {
    font-size: 1.3rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: #fff;
  }

  header h1 span { color: var(--accent); }

  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--muted);
    transition: background 0.3s;
  }
  .status-dot.running { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:0.4 } }

  .status-label { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: var(--muted); }

  nav {
    display: flex;
    gap: 0;
    margin-left: auto;
  }

  nav button {
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 0.5rem 1.1rem;
    cursor: pointer;
    transition: all 0.15s;
    border-right: none;
  }
  nav button:last-child { border-right: 1px solid var(--border); }
  nav button:first-child { border-radius: 4px 0 0 4px; }
  nav button:last-child { border-radius: 0 4px 4px 0; }
  nav button.active, nav button:hover {
    background: var(--accent);
    color: #000;
    border-color: var(--accent);
  }

  main {
    display: grid;
    grid-template-columns: 320px 1fr;
    height: calc(100vh - 61px);
    overflow: hidden;
  }

  /* ── Sidebar ── */
  .sidebar {
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .sidebar-head {
    padding: 1rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  .file-tree {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0;
  }

  .file-tree::-webkit-scrollbar { width: 4px; }
  .file-tree::-webkit-scrollbar-track { background: transparent; }
  .file-tree::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .date-group { }
  .date-header {
    padding: 0.4rem 1rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--accent);
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    user-select: none;
  }
  .date-header:hover { background: var(--border); }
  .date-header .chevron { transition: transform 0.2s; font-size: 0.6rem; }
  .date-header.collapsed .chevron { transform: rotate(-90deg); }

  .file-list { display: flex; flex-direction: column; }
  .date-group.collapsed .file-list { display: none; }

  .file-item {
    padding: 0.35rem 1rem 0.35rem 2rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    transition: background 0.1s;
    color: var(--text);
  }
  .file-item:hover { background: var(--border); }
  .file-item.selected { background: rgba(0,229,255,0.1); color: var(--accent); }
  .file-item .dot {
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--muted); flex-shrink: 0;
  }
  .file-item.processed .dot { background: var(--green); }
  .file-item.night { color: #9b8bff; }

  /* ── Content panels ── */
  .panel {
    display: none;
    flex-direction: column;
    overflow: hidden;
    height: 100%;
  }
  .panel.active { display: flex; }

  .panel-head {
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-shrink: 0;
  }

  .panel-head h2 {
    font-size: 0.85rem;
    font-weight: 600;
    color: #fff;
    flex: 1;
  }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
  }

  /* ── Buttons ── */
  .btn {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 0.5rem 1.2rem;
    border: none;
    border-radius: 3px;
    cursor: pointer;
    transition: all 0.15s;
    font-weight: 600;
    letter-spacing: 0.05em;
  }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { background: #33ecff; }
  .btn-danger  { background: var(--red); color: #fff; }
  .btn-danger:hover { filter: brightness(1.2); }
  .btn-ghost   { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── File detail ── */
  .file-meta {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }
  .meta-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.75rem 1rem;
  }
  .meta-card .label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.25rem;
  }
  .meta-card .value {
    font-size: 0.9rem;
    font-weight: 600;
    color: #fff;
  }

  .action-bar {
    display: flex;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }

  .mode-toggle {
    display: flex;
    gap: 0;
    border: 1px solid var(--border);
    border-radius: 3px;
    overflow: hidden;
  }
  .mode-toggle button {
    background: none;
    border: none;
    color: var(--muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    padding: 0.45rem 0.9rem;
    cursor: pointer;
    transition: all 0.15s;
  }
  .mode-toggle button.active { background: var(--accent2); color: #fff; }

  /* ── Terminal ── */
  .terminal {
    background: #050709;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    line-height: 1.7;
    min-height: 200px;
    max-height: 350px;
    overflow-y: auto;
    color: #7ec8a0;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .terminal::-webkit-scrollbar { width: 4px; }
  .terminal::-webkit-scrollbar-thumb { background: var(--border); }
  .terminal .err { color: var(--red); }
  .terminal .info { color: var(--accent); }

  /* ── Video player ── */
  .video-wrap {
    margin-top: 1.5rem;
    background: #000;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    display: none;
  }
  .video-wrap video { width: 100%; display: block; }

  /* ── Stats ── */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.25rem;
    position: relative;
    overflow: hidden;
  }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent);
  }
  .stat-card .num {
    font-size: 2rem;
    font-weight: 800;
    color: #fff;
    line-height: 1;
    margin-bottom: 0.4rem;
  }
  .stat-card .lbl {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  .section-title {
    font-size: 0.7rem;
    font-family: 'JetBrains Mono', monospace;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
  }
  th {
    text-align: left;
    padding: 0.5rem 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.65rem;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid rgba(30,37,48,0.5);
    color: var(--text);
  }
  tr:hover td { background: var(--surface); }

  /* ── Batch panel ── */
  .batch-form {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }
  .form-group label {
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.4rem;
  }
  .form-group input, .form-group select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 0.5rem 0.75rem;
    border-radius: 3px;
    outline: none;
    transition: border-color 0.15s;
  }
  .form-group input:focus, .form-group select:focus { border-color: var(--accent); }
  .batch-actions { grid-column: 1 / -1; display: flex; gap: 0.75rem; align-items: center; }

  .empty-state {
    text-align: center;
    padding: 4rem 2rem;
    color: var(--muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
  }
  .empty-state .icon { font-size: 3rem; margin-bottom: 1rem; opacity: 0.3; }
</style>
</head>
<body>

<header>
  <h1>Traffic <span>Analyser</span></h1>
  <div style="display:flex;align-items:center;gap:0.5rem">
    <div class="status-dot" id="statusDot"></div>
    <span class="status-label" id="statusLabel">IDLE</span>
  </div>
  <nav>
    <button class="active" onclick="showTab('browse')">Browse</button>
    <button onclick="showTab('batch')">Batch</button>
    <button onclick="showTab('stats')">Stats</button>
  </nav>
</header>

<main>
  <!-- Sidebar: file browser -->
  <aside class="sidebar">
    <div class="sidebar-head">Recordings &nbsp;<span id="fileCount" style="color:var(--accent)"></span></div>
    <div class="file-tree" id="fileTree">
      <div class="empty-state"><div class="icon">📁</div>Loading...</div>
    </div>
  </aside>

  <!-- Browse panel -->
  <section class="panel active" id="panel-browse">
    <div class="panel-head">
      <h2 id="selectedFile">Select a recording</h2>
      <div class="mode-toggle">
        <button class="active" onclick="setMode('auto')" id="modeAuto">AUTO</button>
        <button onclick="setMode('day')"  id="modeDay">DAY</button>
        <button onclick="setMode('night')" id="modeNight">NIGHT</button>
      </div>
      <button class="btn btn-primary" id="btnAnalyse" onclick="runAnalyse()" disabled>▶ Analyse</button>
      <button class="btn btn-danger"  id="btnStop"    onclick="stopJob()"    style="display:none">■ Stop</button>
    </div>
    <div class="panel-body">
      <div id="fileMeta" class="file-meta" style="display:none"></div>
      <div class="terminal" id="terminal">// Select a file and click Analyse&#10;</div>
      <div class="video-wrap" id="videoWrap">
        <video id="videoPlayer" controls></video>
      </div>
    </div>
  </section>

  <!-- Batch panel -->
  <section class="panel" id="panel-batch">
    <div class="panel-head">
      <h2>Batch Processing</h2>
      <button class="btn btn-danger" id="btnBatchStop" onclick="stopJob()" style="display:none">■ Stop</button>
    </div>
    <div class="panel-body">
      <div class="batch-form">
        <div class="form-group">
          <label>Since date (optional)</label>
          <input type="date" id="batchSince">
        </div>
        <div class="form-group">
          <label>Camera (optional)</label>
          <input type="text" id="batchCamera" placeholder="Camera1">
        </div>
        <div class="form-group">
          <label>Limit (optional)</label>
          <input type="number" id="batchLimit" placeholder="e.g. 100">
        </div>
        <div class="form-group">
          <label>Mode</label>
          <select id="batchMode">
            <option value="auto">Auto detect</option>
            <option value="day">Force day</option>
            <option value="night">Force night</option>
          </select>
        </div>
        <div class="batch-actions">
          <button class="btn btn-ghost"    id="btnDryRun"  onclick="runBatch(true)">🔍 Dry Run</button>
          <button class="btn btn-primary"  id="btnBatch"   onclick="runBatch(false)">▶ Run Batch</button>
          <button class="btn btn-ghost"    onclick="runBatch(false, true)">⚡ Force Reprocess</button>
        </div>
      </div>
      <div class="terminal" id="batchTerminal">// Configure options above and click Run Batch&#10;</div>
    </div>
  </section>

  <!-- Stats panel -->
  <section class="panel" id="panel-stats">
    <div class="panel-head">
      <h2>Database Summary</h2>
      <button class="btn btn-ghost" onclick="loadStats()">↻ Refresh</button>
    </div>
    <div class="panel-body" id="statsBody">
      <div class="empty-state"><div class="icon">📊</div>Loading stats...</div>
    </div>
  </section>
</main>

<script>
let selectedPath = null;
let mode = 'auto';
let eventSource = null;
let processedFiles = new Set();

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(tab) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + tab).classList.add('active');
  event.target.classList.add('active');
  if (tab === 'stats') loadStats();
}

// ── Mode selector ─────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  ['Auto','Day','Night'].forEach(x =>
    document.getElementById('mode'+x).classList.remove('active'));
  document.getElementById('mode' + m.charAt(0).toUpperCase() + m.slice(1)).classList.add('active');
}

// ── File tree ─────────────────────────────────────────────────────────────────
async function loadFileTree() {
  const res = await fetch('/api/recordings');
  const data = await res.json();
  processedFiles = new Set(data.processed);
  const tree = document.getElementById('fileTree');
  const count = document.getElementById('fileCount');

  let total = 0;
  let html = '';

  for (const [dateDir, files] of Object.entries(data.tree)) {
    const dayCount = files.length;
    total += dayCount;
    html += `<div class="date-group" id="grp-${dateDir}">
      <div class="date-header" onclick="toggleGroup('${dateDir}')">
        <span class="chevron">▼</span> ${dateDir}
        <span style="margin-left:auto;color:var(--muted)">${dayCount}</span>
      </div>
      <div class="file-list">`;
    for (const f of files) {
      const processed = processedFiles.has(f.path) ? 'processed' : '';
      const night     = f.is_night ? 'night' : '';
      html += `<div class="file-item ${processed} ${night}"
                    onclick="selectFile('${f.path}', '${f.label}', '${dateDir}')">
                 <div class="dot"></div>${f.label}
               </div>`;
    }
    html += `</div></div>`;
  }

  tree.innerHTML = html || '<div class="empty-state"><div class="icon">📭</div>No recordings found</div>';
  count.textContent = total;
}

function toggleGroup(dateDir) {
  const grp = document.getElementById('grp-' + dateDir);
  const hdr = grp.querySelector('.date-header');
  grp.classList.toggle('collapsed');
  hdr.classList.toggle('collapsed');
}

async function selectFile(path, label, dateDir) {
  selectedPath = path;
  document.querySelectorAll('.file-item').forEach(el => el.classList.remove('selected'));
  event.currentTarget.classList.add('selected');
  document.getElementById('selectedFile').textContent = label;
  document.getElementById('btnAnalyse').disabled = false;
  document.getElementById('videoWrap').style.display = 'none';
  document.getElementById('terminal').textContent = '// Ready — click Analyse to process\n';

  // Show metadata
  const res  = await fetch('/api/file_info?path=' + encodeURIComponent(path));
  const info = await res.json();
  const meta = document.getElementById('fileMeta');
  meta.style.display = 'grid';
  meta.innerHTML = `
    <div class="meta-card"><div class="label">Date / Time</div><div class="value">${info.recorded_at}</div></div>
    <div class="meta-card"><div class="label">Duration</div><div class="value">${info.duration}</div></div>
    <div class="meta-card"><div class="label">Resolution</div><div class="value">${info.resolution}</div></div>
    <div class="meta-card"><div class="label">In Database</div><div class="value" style="color:${info.processed ? 'var(--green)' : 'var(--muted)'}">${info.processed ? 'YES' : 'NO'}</div></div>
  `;
}

// ── Job control ───────────────────────────────────────────────────────────────
function setRunning(running) {
  const dot   = document.getElementById('statusDot');
  const lbl   = document.getElementById('statusLabel');
  const btnA  = document.getElementById('btnAnalyse');
  const btnS  = document.getElementById('btnStop');
  const btnB  = document.getElementById('btnBatch');
  const btnBS = document.getElementById('btnBatchStop');
  const btnDR = document.getElementById('btnDryRun');

  dot.className  = 'status-dot' + (running ? ' running' : '');
  lbl.textContent = running ? 'RUNNING' : 'IDLE';
  if (btnA)  btnA.disabled    = running;
  if (btnS)  btnS.style.display  = running ? 'inline-block' : 'none';
  if (btnB)  btnB.disabled    = running;
  if (btnDR) btnDR.disabled   = running;
  if (btnBS) btnBS.style.display = running ? 'inline-block' : 'none';
}

function appendLog(termId, text, cls) {
  const t = document.getElementById(termId);
  const line = document.createElement('span');
  if (cls) line.className = cls;
  line.textContent = text + '\n';
  t.appendChild(line);
  t.scrollTop = t.scrollHeight;
}

function runAnalyse() {
  if (!selectedPath) return;
  document.getElementById('terminal').textContent = '';
  document.getElementById('videoWrap').style.display = 'none';
  setRunning(true);

  fetch('/api/analyse', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ path: selectedPath, mode: mode })
  }).then(r => r.json()).then(data => {
    if (data.error) { appendLog('terminal', data.error, 'err'); setRunning(false); return; }
    streamLog('terminal', data.stream_url, () => {
      setRunning(false);
      loadFileTree();
      // Show annotated video if produced
      fetch('/api/job_output').then(r => r.json()).then(out => {
        if (out.path) {
          document.getElementById('videoWrap').style.display = 'block';
          document.getElementById('videoPlayer').src = '/api/video?path=' + encodeURIComponent(out.path);
        }
      });
    });
  });
}

function runBatch(dryRun, force) {
  const since  = document.getElementById('batchSince').value;
  const camera = document.getElementById('batchCamera').value;
  const limit  = document.getElementById('batchLimit').value;
  const bmode  = document.getElementById('batchMode').value;
  document.getElementById('batchTerminal').textContent = '';
  setRunning(true);

  fetch('/api/batch', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ since, camera, limit, mode: bmode, dry_run: !!dryRun, force: !!force })
  }).then(r => r.json()).then(data => {
    if (data.error) { appendLog('batchTerminal', data.error, 'err'); setRunning(false); return; }
    streamLog('batchTerminal', data.stream_url, () => { setRunning(false); loadFileTree(); });
  });
}

function stopJob() {
  fetch('/api/stop', { method: 'POST' });
}

function streamLog(termId, url, onDone) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(url);
  eventSource.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.done) { eventSource.close(); eventSource = null; onDone(); return; }
    appendLog(termId, d.line, d.err ? 'err' : '');
  };
  eventSource.onerror = () => { eventSource.close(); onDone(); };
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  const res  = await fetch('/api/stats');
  const data = await res.json();
  const body = document.getElementById('statsBody');

  if (data.error) { body.innerHTML = `<div class="empty-state">${data.error}</div>`; return; }

  const s = data.summary;
  const avgSpd = s.avg_speed ? parseFloat(s.avg_speed).toFixed(1) : '—';
  const maxSpd = s.max_speed ? parseFloat(s.max_speed).toFixed(1) : '—';

  body.innerHTML = `
    <div class="section-title">Last 7 days</div>
    <div class="stats-grid">
      <div class="stat-card"><div class="num">${s.total_vehicles || 0}</div><div class="lbl">Vehicles</div></div>
      <div class="stat-card"><div class="num">${avgSpd}</div><div class="lbl">Avg km/h</div></div>
      <div class="stat-card"><div class="num">${maxSpd}</div><div class="lbl">Max km/h</div></div>
      <div class="stat-card"><div class="num">${s.going_right || 0}</div><div class="lbl">→ Right</div></div>
      <div class="stat-card"><div class="num">${s.going_left || 0}</div><div class="lbl">← Left</div></div>
      <div class="stat-card"><div class="num">${data.db_counts.recordings}</div><div class="lbl">Recordings</div></div>
    </div>
    <div class="section-title">Recent recordings</div>
    <table>
      <thead><tr><th>File</th><th>Date/Time</th><th>Vehicles</th><th>Mode</th></tr></thead>
      <tbody>
        ${data.recent.map(r => `<tr>
          <td>${r.filename.split('/').slice(-2).join('/')}</td>
          <td>${r.recorded_at}</td>
          <td>${r.vehicle_count}</td>
          <td>${r.is_night ? '🌙 Night' : '☀️ Day'}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  `;
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadFileTree();
setInterval(() => {
  fetch('/api/status').then(r => r.json()).then(s => {
    setRunning(s.running);
  });
}, 2000);
</script>
</body>
</html>
"""


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/recordings")
def api_recordings():
    """Return file tree grouped by date."""
    tree = {}
    processed_set = set()

    # Get all processed filenames from DB
    try:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filename FROM recordings WHERE processed_at IS NOT NULL")
            for (fn,) in cursor.fetchall():
                processed_set.add(fn)
    except Exception:
        pass

    if not os.path.isdir(RECORDINGS_ROOT):
        return jsonify({"tree": {}, "processed": []})

    for cam in sorted(os.listdir(RECORDINGS_ROOT)):
        cam_path = os.path.join(RECORDINGS_ROOT, cam)
        if not os.path.isdir(cam_path):
            continue
        for date_dir in sorted(os.listdir(cam_path), reverse=True):
            date_path = os.path.join(cam_path, date_dir)
            if not os.path.isdir(date_path):
                continue
            files = []
            for fname in sorted(os.listdir(date_path)):
                if not fname.lower().endswith(".mp4"):
                    continue
                full = os.path.join(date_path, fname)
                files.append({
                    "path":     full,
                    "label":    fname,
                    "is_night": False,
                })
            if files:
                key = f"{cam}/{date_dir}"
                tree[key] = files

    return jsonify({"tree": tree, "processed": list(processed_set)})


@app.route("/api/file_info")
def api_file_info():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"})

    import cv2
    cap = cv2.VideoCapture(path)
    fps    = cap.get(cv2.CAP_PROP_FPS) or 0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    w      = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    dur = frames / fps if fps > 0 else 0

    # Parse datetime from path
    basename = os.path.basename(path)
    parent   = os.path.basename(os.path.dirname(path))
    recorded_at = f"{parent} {basename.replace('.mp4','').replace('-',':')}"

    processed = db.is_already_processed(os.path.abspath(path))

    return jsonify({
        "recorded_at": recorded_at,
        "duration":    f"{dur:.1f}s",
        "resolution":  f"{w}×{h}",
        "fps":         f"{fps:.1f}",
        "processed":   processed,
    })


def _run_job(cmd, job_type, output_path=None):
    """Run a subprocess job, capturing output into _job['log']."""
    global _job
    with _job_lock:
        if _job["running"]:
            return False
        _job = {"running": True, "log": [], "type": job_type,
                "started": datetime.now(), "output": output_path, "pid": None}

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        _job["pid"] = proc.pid
        for line in proc.stdout:
            _job["log"].append(("out", line.rstrip()))
        proc.wait()
    except Exception as e:
        _job["log"].append(("err", str(e)))
    finally:
        _job["running"] = False


@app.route("/api/analyse", methods=["POST"])
def api_analyse():
    global _job
    if _job["running"]:
        return jsonify({"error": "A job is already running"})

    data = request.json
    path = data.get("path", "")
    mode = data.get("mode", "auto")

    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found"})

    # Build output path
    basename   = os.path.splitext(os.path.basename(path))[0]
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_mp4 = os.path.join(ANNOTATED_DIR, f"{basename}_{ts}.mp4")

    cmd = [VENV_PYTHON, ANALYSER,
           "--input", path,
           "--output", output_mp4,
           "--no-show"]
    if mode == "day":   cmd.append("--day")
    if mode == "night": cmd.append("--night")

    threading.Thread(target=_run_job, args=(cmd, "analyse", output_mp4),
                     daemon=True).start()
    time.sleep(0.2)

    return jsonify({"stream_url": "/api/stream"})


@app.route("/api/batch", methods=["POST"])
def api_batch():
    global _job
    if _job["running"]:
        return jsonify({"error": "A job is already running"})

    data    = request.json
    cmd     = [VENV_PYTHON, BATCHER]
    mode    = data.get("mode", "auto")
    since   = data.get("since", "")
    camera  = data.get("camera", "")
    limit   = data.get("limit", "")
    dry_run = data.get("dry_run", False)
    force   = data.get("force", False)

    if since:   cmd += ["--since",  since]
    if camera:  cmd += ["--camera", camera]
    if limit:   cmd += ["--limit",  str(limit)]
    if mode == "day":   cmd.append("--day")
    if mode == "night": cmd.append("--night")
    if dry_run: cmd.append("--dry-run")
    if force:   cmd.append("--force")
    cmd.append("--save-db") if not dry_run else None

    threading.Thread(target=_run_job, args=(cmd, "batch"),
                     daemon=True).start()
    time.sleep(0.2)

    return jsonify({"stream_url": "/api/stream"})


@app.route("/api/stream")
def api_stream():
    """SSE stream of current job output."""
    def generate():
        sent = 0
        while True:
            log   = _job["log"]
            running = _job["running"]
            while sent < len(log):
                kind, line = log[sent]
                yield f"data: {json.dumps({'line': line, 'err': kind=='err'})}\n\n"
                sent += 1
            if not running and sent >= len(log):
                yield f"data: {json.dumps({'done': True})}\n\n"
                return
            time.sleep(0.1)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    import signal
    if _job["running"] and _job.get("pid"):
        try:
            os.kill(_job["pid"], signal.SIGTERM)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/job_output")
def api_job_output():
    path = _job.get("output")
    if path and os.path.exists(path):
        return jsonify({"path": path})
    return jsonify({"path": None})


@app.route("/api/video")
def api_video():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path):
        return "Not found", 404
    return send_file(path, mimetype="video/mp4")


@app.route("/api/status")
def api_status():
    return jsonify({"running": _job["running"], "type": _job.get("type")})


@app.route("/api/stats")
def api_stats():
    try:
        summary = db.get_summary(days=7)
        # Convert Decimal to float for JSON serialisation
        for k, v in summary.items():
            if v is not None and hasattr(v, '__float__'):
                summary[k] = float(v)

        with db.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT filename, recorded_at, vehicle_count, is_night
                FROM recordings
                ORDER BY recorded_at DESC
                LIMIT 20
            """)
            recent = cursor.fetchall()
            for r in recent:
                if r["recorded_at"]:
                    r["recorded_at"] = str(r["recorded_at"])
                r["is_night"] = bool(r["is_night"])

            cursor.execute("SELECT COUNT(*) AS cnt FROM recordings")
            rec_count = cursor.fetchone()["cnt"]
            cursor.execute("SELECT COUNT(*) AS cnt FROM vehicles")
            veh_count = cursor.fetchone()["cnt"]

        return jsonify({
            "summary":   summary,
            "recent":    recent,
            "db_counts": {"recordings": rec_count, "vehicles": veh_count},
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Traffic Analyser UI — http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
