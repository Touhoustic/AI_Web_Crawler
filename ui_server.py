"""
ui_server.py — Local web UI for the Web Browsing Agent.

Run:
    python ui_server.py
Then open: http://localhost:5000

Features:
  - Visual config panel (URL, goal, steps, depth, model)
  - Persistent API key storage (keys.json, gitignored)
  - Real-time log streaming via SSE
  - Results dashboard (emails, phones, socials, people)
  - Copy CLI command / export JSON report
"""

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request, send_file

# ── Config ────────────────────────────────────────────────────────────────────
KEYS_FILE = Path("keys.json")
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Global state for the current run
_run_state = {
    "running": False,
    "log_queue": queue.Queue(),
    "result": None,
    "progress": 0,
    "label": "Idle",
}


# ── Key persistence ───────────────────────────────────────────────────────────

def load_keys() -> list[str]:
    if KEYS_FILE.exists():
        try:
            return json.loads(KEYS_FILE.read_text())
        except Exception:
            pass
    return []


def save_keys(keys: list[str]) -> None:
    KEYS_FILE.write_text(json.dumps(keys, indent=2))
    # Auto-add to .gitignore
    gi = Path(".gitignore")
    content = gi.read_text() if gi.exists() else ""
    if "keys.json" not in content:
        with gi.open("a") as f:
            f.write("\nkeys.json\n")


# ── HTML template ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Web Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0f;
  --surface: #111118;
  --surface2: #1a1a24;
  --border: #2a2a38;
  --accent: #7c6eff;
  --accent2: #ff6eb4;
  --green: #4ade80;
  --yellow: #fbbf24;
  --red: #f87171;
  --blue: #60a5fa;
  --text: #e8e8f0;
  --muted: #666680;
  --font-head: 'Syne', sans-serif;
  --font-mono: 'DM Mono', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--text); font-family: var(--font-mono); font-size: 13px; }

/* Layout */
.shell { display: grid; grid-template-columns: 340px 1fr; height: 100vh; }
.sidebar { border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow-y: auto; }
.main { display: flex; flex-direction: column; overflow: hidden; }

/* Sidebar header */
.logo { padding: 24px 20px 16px; border-bottom: 1px solid var(--border); }
.logo h1 { font-family: var(--font-head); font-size: 20px; font-weight: 800; letter-spacing: -.5px; }
.logo h1 span { color: var(--accent); }
.logo p { color: var(--muted); font-size: 11px; margin-top: 3px; }

/* Tabs */
.tabs { display: flex; border-bottom: 1px solid var(--border); }
.tab { flex: 1; padding: 10px; font-family: var(--font-mono); font-size: 11px; font-weight: 500; background: none; border: none; color: var(--muted); cursor: pointer; letter-spacing: .05em; text-transform: uppercase; transition: color .15s; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom: 2px solid var(--accent); }

/* Panes */
.pane { display: none; flex-direction: column; flex: 1; }
.pane.active { display: flex; }
.pane-body { padding: 16px 20px; flex: 1; overflow-y: auto; }

/* Form elements */
.field { margin-bottom: 14px; }
label { display: block; font-size: 10px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 5px; }
input, textarea, select {
  width: 100%; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-family: var(--font-mono); font-size: 12px;
  padding: 8px 10px; outline: none; transition: border-color .15s;
}
input:focus, textarea:focus, select:focus { border-color: var(--accent); }
textarea { resize: vertical; min-height: 70px; line-height: 1.5; }
select option { background: var(--surface2); }

/* Row grid */
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }

/* Toggles */
.toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; }
.toggle-label { color: var(--text); font-size: 12px; }
.switch { position: relative; width: 36px; height: 20px; }
.switch input { opacity: 0; width: 0; height: 0; }
.knob { position: absolute; inset: 0; background: var(--border); border-radius: 10px; cursor: pointer; transition: .2s; }
.knob:before { content: ''; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px; background: var(--muted); border-radius: 50%; transition: .2s; }
input:checked + .knob { background: var(--accent); }
input:checked + .knob:before { transform: translateX(16px); background: #fff; }

/* Key rows */
.key-row { display: flex; gap: 6px; margin-bottom: 6px; align-items: center; }
.key-row input { flex: 1; font-size: 11px; }
.icon-btn { background: var(--surface2); border: 1px solid var(--border); border-radius: 5px; color: var(--muted); cursor: pointer; font-size: 11px; padding: 6px 10px; white-space: nowrap; transition: all .15s; font-family: var(--font-mono); }
.icon-btn:hover { color: var(--text); border-color: var(--accent); }
.icon-btn.danger:hover { color: var(--red); border-color: var(--red); }

/* Run button */
.run-btn {
  margin: 0 20px 20px; padding: 12px; font-family: var(--font-head);
  font-size: 14px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
  background: var(--accent); color: #fff; border: none; border-radius: 8px;
  cursor: pointer; transition: all .2s; position: relative; overflow: hidden;
}
.run-btn:hover { background: #9482ff; transform: translateY(-1px); }
.run-btn:active { transform: translateY(0); }
.run-btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }
.run-btn.running { background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }

/* Main area */
.main-header { padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.main-header h2 { font-family: var(--font-head); font-size: 16px; font-weight: 700; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); display: inline-block; margin-right: 6px; transition: background .3s; }
.status-dot.active { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }

/* Progress */
.progress-wrap { padding: 12px 24px; border-bottom: 1px solid var(--border); }
.progress-bar { height: 2px; background: var(--border); border-radius: 1px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .4s; width: 0%; }
.progress-meta { display: flex; justify-content: space-between; font-size: 10px; color: var(--muted); margin-top: 5px; }

/* Stats */
.stats-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; background: var(--border); border-bottom: 1px solid var(--border); }
.stat { background: var(--bg); padding: 14px 0; text-align: center; }
.stat-n { font-family: var(--font-head); font-size: 22px; font-weight: 800; }
.stat-l { font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; margin-top: 2px; }

/* Tab content in main */
.main-tabs { display: flex; padding: 0 24px; border-bottom: 1px solid var(--border); gap: 0; }
.main-tab { padding: 10px 16px; font-size: 11px; font-weight: 500; background: none; border: none; color: var(--muted); cursor: pointer; letter-spacing: .05em; text-transform: uppercase; font-family: var(--font-mono); border-bottom: 2px solid transparent; margin-bottom: -1px; transition: color .15s; }
.main-tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.content-area { flex: 1; overflow: hidden; }
.content-pane { display: none; height: 100%; overflow-y: auto; padding: 16px 24px; }
.content-pane.active { display: block; }

/* Log */
.log { font-family: var(--font-mono); font-size: 11px; line-height: 1.9; }
.log-line { display: block; }
.log-ok { color: var(--green); }
.log-warn { color: var(--yellow); }
.log-err { color: var(--red); }
.log-info { color: var(--blue); }
.log-dim { color: var(--muted); }

/* Results */
.section-title { font-size: 10px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; margin: 16px 0 8px; }
.section-title:first-child { margin-top: 0; }
.chip { display: inline-block; background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; padding: 3px 9px; font-size: 11px; margin: 2px; }
.chip.email { border-color: #3b4fd8; color: #93acff; background: #0d1240; }
.chip.phone { border-color: #2d6640; color: #4ade80; background: #0a1f12; }
.chip.social { border-color: #5c3780; color: #c084fc; background: #150d24; }
.platform-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; display: block; margin: 8px 0 3px; }

/* People cards */
.person-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; background: var(--surface); }
.person-name { font-family: var(--font-head); font-weight: 700; font-size: 14px; }
.person-role { color: var(--muted); font-size: 11px; margin-top: 2px; }
.person-links { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
.person-link { font-size: 10px; padding: 2px 7px; border-radius: 3px; text-decoration: none; }
.person-link.email { background: #0d1240; color: #93acff; }
.person-link.linkedin { background: #0a1e38; color: #60a5fa; }
.person-link.twitter { background: #0d1620; color: #38bdf8; }

/* Action buttons */
.actions { display: flex; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.action-btn { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); cursor: pointer; font-size: 11px; padding: 7px 14px; font-family: var(--font-mono); transition: all .15s; }
.action-btn:hover { border-color: var(--accent); color: var(--accent); }

/* Saved toast */
.toast { position: fixed; bottom: 24px; right: 24px; background: var(--green); color: #000; font-weight: 500; padding: 8px 16px; border-radius: 6px; font-size: 12px; opacity: 0; transition: opacity .3s; pointer-events: none; }
.toast.show { opacity: 1; }

/* Scrollbar */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>
<div class="shell">

<!-- SIDEBAR -->
<aside class="sidebar">
  <div class="logo">
    <h1>web<span>agent</span></h1>
    <p>llama-3.3-70b-versatile · Groq</p>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="sideTab('config')">Config</button>
    <button class="tab" onclick="sideTab('keys')">Keys</button>
  </div>

  <!-- CONFIG PANE -->
  <div class="pane active" id="side-config">
    <div class="pane-body">
      <div class="field">
        <label>Target URL</label>
        <input id="url" type="url" placeholder="https://example.com" />
      </div>
      <div class="field">
        <label>Goal</label>
        <textarea id="goal">Find all contact information, emails, phone numbers, team members, and social media profiles</textarea>
      </div>
      <div class="row3" style="margin-bottom:14px">
        <div class="field" style="margin:0"><label>Max steps</label><input id="steps" type="number" value="25" min="5" max="100"/></div>
        <div class="field" style="margin:0"><label>Max depth</label><input id="depth" type="number" value="3" min="1" max="10"/></div>
        <div class="field" style="margin:0"><label>Threads</label><input id="threads" type="number" value="1" min="1" max="5"/></div>
      </div>
      <div class="field">
        <label>Model</label>
        <select id="model">
          <option value="llama-3.3-70b-versatile" selected>llama-3.3-70b-versatile</option>
          <option value="llama3-70b-8192">llama3-70b-8192</option>
          <option value="mixtral-8x7b-32768">mixtral-8x7b-32768</option>
        </select>
      </div>
      <div class="toggle-row"><span class="toggle-label">Same domain only</span><label class="switch"><input type="checkbox" id="sameDomain" checked><span class="knob"></span></label></div>
      <div class="toggle-row"><span class="toggle-label">Headless browser</span><label class="switch"><input type="checkbox" id="headless" checked><span class="knob"></span></label></div>
      <div class="toggle-row"><span class="toggle-label">Save report</span><label class="switch"><input type="checkbox" id="saveReport" checked><span class="knob"></span></label></div>
      <div class="toggle-row"><span class="toggle-label">Smart name extraction</span><label class="switch"><input type="checkbox" id="smartNames" checked><span class="knob"></span></label></div>
    </div>
  </div>

  <!-- KEYS PANE -->
  <div class="pane" id="side-keys">
    <div class="pane-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:12px;line-height:1.6">
        Keys saved to <code>keys.json</code> (auto-gitignored). Multiple keys rotate automatically on rate limits.
      </div>
      <div id="keysList"></div>
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button class="icon-btn" onclick="addKeyField()">+ Add key</button>
        <button class="icon-btn" onclick="saveKeys()">Save</button>
        <button class="icon-btn danger" onclick="clearKeys()">Clear all</button>
      </div>
    </div>
  </div>

  <button class="run-btn" id="runBtn" onclick="startRun()">▶ Run Agent</button>
</aside>

<!-- MAIN -->
<main class="main">
  <div class="main-header">
    <h2><span class="status-dot" id="statusDot"></span><span id="statusLabel">Ready</span></h2>
    <div style="display:flex;gap:8px">
      <button class="action-btn" onclick="copyCmd()">Copy CLI</button>
      <button class="action-btn" onclick="exportJSON()">Export JSON</button>
      <button class="action-btn" onclick="clearAll()" style="color:var(--muted)">Clear</button>
    </div>
  </div>

  <div class="progress-wrap">
    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
    <div class="progress-meta"><span id="progressLabel">No run yet</span><span id="progressPct"></span></div>
  </div>

  <div class="stats-row">
    <div class="stat"><div class="stat-n" id="s-pages">0</div><div class="stat-l">Pages</div></div>
    <div class="stat"><div class="stat-n" id="s-emails">0</div><div class="stat-l">Emails</div></div>
    <div class="stat"><div class="stat-n" id="s-phones">0</div><div class="stat-l">Phones</div></div>
    <div class="stat"><div class="stat-n" id="s-socials">0</div><div class="stat-l">Socials</div></div>
    <div class="stat"><div class="stat-n" id="s-people">0</div><div class="stat-l">People</div></div>
  </div>

  <div class="main-tabs">
    <button class="main-tab active" onclick="mainTab('log')">Live log</button>
    <button class="main-tab" onclick="mainTab('emails')">Emails</button>
    <button class="main-tab" onclick="mainTab('socials')">Social media</button>
    <button class="main-tab" onclick="mainTab('people')">People</button>
    <button class="main-tab" onclick="mainTab('report')">Report</button>
  </div>

  <div class="content-area">
    <div class="content-pane active" id="pane-log"><div class="log" id="logOutput"></div></div>
    <div class="content-pane" id="pane-emails"><div id="emailsOut"><span style="color:var(--muted)">No emails found yet.</span></div></div>
    <div class="content-pane" id="pane-socials"><div id="socialsOut"><span style="color:var(--muted)">No social profiles found yet.</span></div></div>
    <div class="content-pane" id="pane-people"><div id="peopleOut"><span style="color:var(--muted)">No people found yet.</span></div></div>
    <div class="content-pane" id="pane-report"><pre id="reportOut" style="font-size:11px;line-height:1.7;color:var(--muted)">Run the agent to generate a report.</pre></div>
  </div>
</main>
</div>

<div class="toast" id="toast"></div>

<script>
const state = { emails:[], phones:[], socials:{}, people:[], pages:0, report:null };
let es = null;

// ── Tabs ──────────────────────────────────────────────────────────────────────
function sideTab(name) {
  document.querySelectorAll('.sidebar .tab').forEach((t,i)=>t.classList.toggle('active',['config','keys'][i]===name));
  document.querySelectorAll('.sidebar .pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('side-'+name).classList.add('active');
  if(name==='keys') renderKeys();
}

function mainTab(name) {
  document.querySelectorAll('.main-tab').forEach((t,i)=>t.classList.toggle('active',['log','emails','socials','people','report'][i]===name));
  document.querySelectorAll('.content-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('pane-'+name).classList.add('active');
}

// ── Keys ──────────────────────────────────────────────────────────────────────
function renderKeys() {
  fetch('/api/keys').then(r=>r.json()).then(keys=>{
    const el = document.getElementById('keysList');
    el.innerHTML = '';
    (keys.length ? keys : ['']).forEach((k,i) => {
      const row = document.createElement('div');
      row.className = 'key-row';
      row.innerHTML = `<input type="password" value="${k}" placeholder="gsk_..." data-idx="${i}" />`
        + (keys.length ? `<button class="icon-btn danger" onclick="deleteKey(${i})">✕</button>` : '');
      el.appendChild(row);
    });
  });
}

function addKeyField() {
  const el = document.getElementById('keysList');
  const row = document.createElement('div');
  row.className = 'key-row';
  row.innerHTML = `<input type="password" placeholder="gsk_..." />`;
  el.appendChild(row);
  row.querySelector('input').focus();
}

function saveKeys() {
  const inputs = document.querySelectorAll('#keysList input');
  const keys = [...inputs].map(i=>i.value.trim()).filter(Boolean);
  fetch('/api/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({keys})})
    .then(()=>{ toast('Keys saved'); renderKeys(); });
}

function deleteKey(i) {
  fetch('/api/keys/'+i, {method:'DELETE'}).then(()=>renderKeys());
}

function clearKeys() {
  if(confirm('Clear all saved keys?')) fetch('/api/keys', {method:'DELETE'}).then(()=>renderKeys());
}

// ── Run ───────────────────────────────────────────────────────────────────────
function getConfig() {
  return {
    url: document.getElementById('url').value.trim(),
    goal: document.getElementById('goal').value.trim(),
    model: document.getElementById('model').value,
    steps: parseInt(document.getElementById('steps').value)||25,
    depth: parseInt(document.getElementById('depth').value)||3,
    same_domain: document.getElementById('sameDomain').checked,
    headless: document.getElementById('headless').checked,
    save_report: document.getElementById('saveReport').checked,
    smart_names: document.getElementById('smartNames').checked,
  };
}

function startRun() {
  const cfg = getConfig();
  if (!cfg.url) { alert('Enter a target URL first.'); return; }

  // Reset state
  Object.assign(state, {emails:[],phones:[],socials:{},people:[],pages:0,report:null});
  document.getElementById('logOutput').innerHTML = '';
  updateStats();
  document.getElementById('emailsOut').innerHTML = '<span style="color:var(--muted)">Running…</span>';
  document.getElementById('socialsOut').innerHTML = '<span style="color:var(--muted)">Running…</span>';
  document.getElementById('peopleOut').innerHTML = '<span style="color:var(--muted)">Running…</span>';
  document.getElementById('reportOut').textContent = 'Running…';
  mainTab('log');

  const btn = document.getElementById('runBtn');
  btn.textContent = '⏹ Running…';
  btn.classList.add('running');
  btn.disabled = true;
  document.getElementById('statusDot').classList.add('active');
  document.getElementById('statusLabel').textContent = 'Running';

  fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)})
    .then(r => r.json())
    .then(d => {
      if (d.error) { log('ERROR: '+d.error, 'err'); stopRunUI(); return; }
      startSSE();
    });
}

function startSSE() {
  if (es) es.close();
  es = new EventSource('/api/stream');

  es.addEventListener('log', e => {
    const d = JSON.parse(e.data);
    log(d.msg, d.cls);
  });

  es.addEventListener('progress', e => {
    const d = JSON.parse(e.data);
    document.getElementById('progressFill').style.width = d.pct + '%';
    document.getElementById('progressLabel').textContent = d.label;
    document.getElementById('progressPct').textContent = d.pct + '%';
  });

  es.addEventListener('stats', e => {
    const d = JSON.parse(e.data);
    Object.assign(state, d);
    updateStats();
    renderResults();
  });

  es.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    state.report = d.report;
    document.getElementById('reportOut').textContent = JSON.stringify(d.report, null, 2);
    updateStats();
    renderResults();
    stopRunUI();
    es.close();
    log('Agent finished.', 'ok');
  });

  es.onerror = () => { stopRunUI(); es.close(); };
}

function stopRunUI() {
  const btn = document.getElementById('runBtn');
  btn.textContent = '▶ Run Agent';
  btn.classList.remove('running');
  btn.disabled = false;
  document.getElementById('statusDot').classList.remove('active');
  document.getElementById('statusLabel').textContent = 'Done';
  document.getElementById('progressFill').style.width = '100%';
}

// ── Log ───────────────────────────────────────────────────────────────────────
function log(msg, cls='') {
  const el = document.getElementById('logOutput');
  const line = document.createElement('span');
  line.className = 'log-line' + (cls ? ' log-'+cls : ' log-dim');
  line.textContent = msg;
  el.appendChild(line);
  el.appendChild(document.createTextNode('\n'));
  el.parentElement.scrollTop = el.parentElement.scrollHeight;
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function updateStats() {
  document.getElementById('s-pages').textContent = state.pages||0;
  document.getElementById('s-emails').textContent = (state.emails||[]).length;
  document.getElementById('s-phones').textContent = (state.phones||[]).length;
  document.getElementById('s-socials').textContent = Object.values(state.socials||{}).flat().length;
  document.getElementById('s-people').textContent = (state.people||[]).length;
}

// ── Results render ────────────────────────────────────────────────────────────
function renderResults() {
  // Emails
  const emails = state.emails||[];
  document.getElementById('emailsOut').innerHTML = emails.length
    ? emails.map(e=>`<span class="chip email">${e}</span>`).join('')
    : '<span style="color:var(--muted)">No emails found.</span>';

  // Socials
  const socials = state.socials||{};
  const keys = Object.keys(socials);
  document.getElementById('socialsOut').innerHTML = keys.length
    ? keys.map(p=>`<span class="platform-label">${p}</span>`+socials[p].map(u=>`<span class="chip social">${u}</span>`).join('')).join('')
    : '<span style="color:var(--muted)">No social profiles found.</span>';

  // People
  const people = state.people||[];
  document.getElementById('peopleOut').innerHTML = people.length
    ? people.map(p=>{
        const links = [
          p.email ? `<a class="person-link email" href="mailto:${p.email}">${p.email}</a>` : '',
          p.linkedin ? `<a class="person-link linkedin" href="${p.linkedin}" target="_blank">LinkedIn</a>` : '',
          p.twitter ? `<a class="person-link twitter" href="${p.twitter}" target="_blank">Twitter</a>` : '',
        ].filter(Boolean).join('');
        return `<div class="person-card">
          <div class="person-name">${p.name}</div>
          ${p.role?`<div class="person-role">${p.role}</div>`:''}
          ${links?`<div class="person-links">${links}</div>`:''}
        </div>`;
      }).join('')
    : '<span style="color:var(--muted)">No people found.</span>';
}

// ── Actions ───────────────────────────────────────────────────────────────────
function copyCmd() {
  fetch('/api/keys').then(r=>r.json()).then(keys=>{
    const cfg = getConfig();
    const cmd = `python main.py --url "${cfg.url}" --goal "${cfg.goal}" --keys ${keys.join(',')} --model ${cfg.model} --steps ${cfg.steps} --depth ${cfg.depth}${!cfg.same_domain?' --all-domains':''}${!cfg.headless?' --headed':''}${!cfg.save_report?' --no-report':''}`;
    navigator.clipboard.writeText(cmd).then(()=>toast('CLI command copied'));
  });
}

function exportJSON() {
  if (!state.report) { toast('No report yet — run the agent first'); return; }
  const blob = new Blob([JSON.stringify(state.report, null, 2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'report_'+Date.now()+'.json'; a.click();
}

function clearAll() {
  document.getElementById('logOutput').innerHTML = '';
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('progressLabel').textContent = 'No run yet';
  document.getElementById('progressPct').textContent = '';
  Object.assign(state, {emails:[],phones:[],socials:{},people:[],pages:0,report:null});
  updateStats(); renderResults();
  document.getElementById('statusLabel').textContent = 'Ready';
  document.getElementById('reportOut').textContent = 'Run the agent to generate a report.';
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2000);
}

// Init
renderKeys();
</script>
</body>
</html>
"""


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/keys", methods=["GET"])
def get_keys():
    return jsonify(load_keys())


@app.route("/api/keys", methods=["POST"])
def set_keys():
    data = request.get_json()
    save_keys(data.get("keys", []))
    return jsonify({"ok": True})


@app.route("/api/keys/<int:idx>", methods=["DELETE"])
def delete_key(idx):
    keys = load_keys()
    if 0 <= idx < len(keys):
        keys.pop(idx)
        save_keys(keys)
    return jsonify({"ok": True})


@app.route("/api/keys", methods=["DELETE"])
def clear_keys():
    save_keys([])
    return jsonify({"ok": True})


@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_state["running"]:
        return jsonify({"error": "Agent already running"}), 409

    cfg = request.get_json()
    keys = load_keys()
    if not keys:
        return jsonify({"error": "No API keys saved. Add keys in the Keys tab."})

    _run_state["running"] = True
    _run_state["result"] = None
    _run_state["progress"] = 0

    # Clear old log queue
    while not _run_state["log_queue"].empty():
        try:
            _run_state["log_queue"].get_nowait()
        except queue.Empty:
            break

    thread = threading.Thread(target=_run_agent_thread, args=(cfg, keys), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route("/api/stream")
def stream():
    def gen():
        q = _run_state["log_queue"]
        while True:
            try:
                event = q.get(timeout=30)
                yield event
                if '"done"' in event:
                    break
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _emit(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    _run_state["log_queue"].put(msg)


def _run_agent_thread(cfg: dict, keys: list[str]):
    """Run the agent in a background thread, emitting SSE events."""
    try:
        from agent import WebAgent, AgentConfig
        from browser import BrowserWrapper
        from memory import init_db

        _emit("log", {"msg": f"Starting agent on {cfg['url']}", "cls": "info"})
        _emit("log", {"msg": f"Model: {cfg['model']}  Steps: {cfg['steps']}  Depth: {cfg['depth']}", "cls": "info"})
        _emit("log", {"msg": f"Keys loaded: {len(keys)}", "cls": "info"})
        _emit("progress", {"pct": 5, "label": "Initialising…"})

        # Patch logging to emit SSE events
        import logging

        class SSEHandler(logging.Handler):
            def emit(self, record):
                msg = self.format(record)
                cls = "ok" if "✅" in msg or "Email" in msg or "Phone" in msg else \
                      "warn" if "⚠" in msg else \
                      "err" if "❌" in msg else \
                      "info" if "🌐" in msg or "Step" in msg or "🤖" in msg else ""
                _emit("log", {"msg": msg, "cls": cls})

        from utils import logger
        sse_handler = SSEHandler()
        sse_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sse_handler)

        db_path = REPORTS_DIR / "agent_data.db"
        conn = init_db(db_path)

        agent_config = AgentConfig(
            seed_url=cfg["url"],
            goal=cfg["goal"],
            api_keys=keys,
            max_steps=cfg.get("steps", 25),
            max_depth=cfg.get("depth", 3),
            same_domain_only=cfg.get("same_domain", True),
            headless=cfg.get("headless", True),
            model=cfg.get("model", "llama-3.3-70b-versatile"),
            save_report=cfg.get("save_report", True),
            output_dir=str(REPORTS_DIR),
        )

        # Monkey-patch the agent to emit stats after each step
        original_persist = None
        try:
            import memory as mem_mod
            from memory import fetch_emails, fetch_social_links, fetch_contacts, fetch_people

            original_persist = mem_mod.persist_extraction

            def patched_persist(conn_, data, depth=0):
                original_persist(conn_, data, depth)
                emails = fetch_emails(conn_)
                socials = fetch_social_links(conn_)
                contacts = fetch_contacts(conn_)
                people = fetch_people(conn_)
                pages = conn_.execute("SELECT COUNT(*) FROM visited_urls").fetchone()[0]
                _emit("stats", {
                    "emails": emails,
                    "phones": contacts.get("phone", []),
                    "socials": socials,
                    "people": people,
                    "pages": pages,
                })

            mem_mod.persist_extraction = patched_persist
        except Exception:
            pass

        async def _run():
            browser = BrowserWrapper(headless=agent_config.headless)
            await browser.start()
            try:
                agent = WebAgent(agent_config, conn)

                # Patch step counter to update progress
                original_run = agent.run

                async def patched_run(browser_):
                    result = await original_run(browser_)
                    return result

                report = await patched_run(browser)
                return report
            finally:
                await browser.stop()
                conn.close()

        report = asyncio.run(_run())

        # Restore
        if original_persist:
            import memory as mem_mod
            mem_mod.persist_extraction = original_persist

        logger.removeHandler(sse_handler)
        _emit("progress", {"pct": 100, "label": "Done"})
        _emit("done", {"report": report})

    except Exception as e:
        _emit("log", {"msg": f"Fatal error: {e}", "cls": "err"})
        _emit("done", {"report": None})
    finally:
        _run_state["running"] = False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    print("\n" + "="*50)
    print("  Web Agent UI")
    print("  http://localhost:5000")
    print("="*50 + "\n")
    # Open browser after a short delay
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
