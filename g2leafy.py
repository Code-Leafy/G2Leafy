import os
import re
import sys
import time
import json
import socket
import copy
import uuid
import shutil
import base64
import asyncio
import subprocess
import threading
import urllib.request
import urllib.parse
import signal
import ssl
import hashlib
import hmac
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ============================================================================
#  G2Leafy panel  -  split architecture
# ----------------------------------------------------------------------------
#  This file (backend.py) contains ALL the server-side logic. The UI lives in
#  its own file (index.html) which is loaded from disk at request time, so you
#  can edit the HTML/CSS/JS freely and just refresh the browser - no backend
#  restart required ("hot-swap" frontend).
#
#  Conversely the backend is "hot-swappable": the panel talks ONLY to the HTTP
#  API documented below. Any replacement backend (different proxy core, different
#  host, rewrite in another language) only has to honour this contract and the
#  UI keeps working unchanged.
#
#  API CONTRACT  (frontend <-> backend)
#  ----------------------------------------------------------------------
#   GET  /                         -> panel HTML (index.html)
#   GET  /panel /login /admin      -> panel HTML  (aliases)
#   GET  /sub/<token>              -> subscription: base64 for proxy clients,
#                                     pretty HTML page for browsers
#   GET  /api/state                -> { ok, state, portDomain, logs, <telemetry*> }
#   PUT  /api/state                <- { state, reason }   (push UI changes)
#   POST /api/login                <- { pass }            -> sets session cookie
#   POST /api/setup                <- { pass }            -> first-run password
#   POST /api/action               <- { action: start|stop|restart|clear_logs }
#   POST /api/donate               -> donate a config to the community pool
#   POST /api/backup               -> snapshot panel_state.json
#   GET  /api/sub/link/<id>        -> { ok, link }
#   GET  /api/config               -> { ok, config }  (live generated xray config)
#
#  *telemetry fields returned by GET /api/state (flat, top-level keys):
#     totalRxGb, totalTxGb, speedDownMbps, speedUpMbps, connections, cpuPct,
#     ramMb, ramTotalMb, diskUsedGb, diskTotalGb, loadAvg, xrayUptimeSec,
#     xrayRunning, quotaTotalH, quotaUsedH, quotaRemainH, ipCity, ipCountry,
#     ipIpv4, certSha256, githubUser, webDomain, tcpCc, ipProvider,
#     baseQuotaHours, hoursPerDollar, cpuCores
#
#  FRONTEND SYNC LOOP (already implemented in index.html):
#     - poll  GET  /api/state  every 2s  -> applyPanelState() + telemetry
#     - push  PUT  /api/state  debounced -> serializePanelState()
# ============================================================================

LOCAL_VERSION = "3.5.0"
# Shown in the panel footer + start banner so you can confirm you're running
# THIS build (not the old single-file g2leafy.py). Bump on every change.
BUILD_ID = "split-2026-08-08-hardcoded-uptime"
AUTO_UPDATE = True
UPSTREAM_REPO = "Code-Leafy/G2Leafy"
RAW_BASE = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/refs/heads/main/"

DONATE_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwJjAYF_G4PiXRC0w-g0RrEzskBn_2Mg_xz2MiZP1aJE6Vzpc0P8cRqu4fCESsw0SX4Ig/exec"
DONATE_SECRET = ""
DONATE_IP = "20.120.56.11"
DONATE_HEARTBEAT_SEC = 240
DONATE_TTL_SEC = 720
DONATE_QUOTA_GRACE_SEC = 600

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# The panel UI lives here and is (re)loaded from disk so it can be edited
# without restarting the backend. Must sit next to this file.
INDEX_HTML_FILE = os.path.join(BASE_DIR, "index.html")

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PANEL_STATE_FILE = os.path.join(DATA_DIR, "panel_state.json")
UUID_FILE = os.path.join(DATA_DIR, "uuid.txt")
XRAY_LOG = os.path.join(LOG_DIR, "xray.log")
XRAY_ACCESS_LOG = os.path.join(LOG_DIR, "access.log")
SYSTEM_LOG = os.path.join(LOG_DIR, "system.log")
XRAY_BIN = "/usr/local/bin/xray"

XRAY_PORT = 443
XRAY_XHTTP_PORT = 10001
XRAY_WS_PORT = 10003
WEB_PORT = 8080
API_PORT = 10085

# Quota model - hardcoded simple model: 60h free + $1 = 20h
# Single source of truth - exposed via /api/state so frontend never hard-codes.
BASE_QUOTA_HOURS = 60
HOURS_PER_DOLLAR = 20

for d in [DATA_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

state_lock = threading.Lock()
file_lock = threading.RLock()
engine_running = True
ports_thread_active = False
ports_thread_lock = threading.Lock()

# Multiplexer guard
_mux_started = False
_mux_lock = threading.Lock()

# XRay start guard
_xray_start_lock = threading.Lock()

state = {
    "total_down": 0, "total_up": 0, "uptime_sec": 0,
    "xray_uptime_sec": 0,
    "speed_down_bps": 0, "speed_up_bps": 0, "cpu_pct": 0.0,
    "mem_used_mb": 0, "mem_total_mb": 4096,
    "disk_used_gb": 0, "disk_total_gb": 0,
    "load_avg": [0,0,0], "is_xray_running": False,
    "client_usage_bytes": {},
    "ip_city": "N/A", "ip_country": "N/A", "ip_ipv4": "N/A", "ip_org": "",
    "donate_active": False, "donate_last": 0
}

try:
    CODESPACE_NAME = os.environ.get("CODESPACE_NAME")
    if not CODESPACE_NAME:
        CODESPACE_NAME = subprocess.check_output(["gh", "codespace", "list", "--limit", "1", "--json", "name", "--jq", ".[0].name"], text=True, stderr=subprocess.DEVNULL).strip()
except Exception:
    CODESPACE_NAME = os.uname().nodename

if CODESPACE_NAME and '\n' in CODESPACE_NAME:
    CODESPACE_NAME = CODESPACE_NAME.split('\n')[-1].strip()

PORT_DOMAIN = f"{CODESPACE_NAME}-{XRAY_PORT}.app.github.dev"
WEB_DOMAIN = f"{CODESPACE_NAME}-{WEB_PORT}.app.github.dev"
GITHUB_USER = os.environ.get("GITHUB_USER", CODESPACE_NAME.split('-')[0] if '-' in CODESPACE_NAME else "User")
PANEL_PASSWORD = os.environ.get("PASS", "")

_cached_cert_sha = ""
_cached_cert_time = 0

# Auth - stateless HMAC session (survives process restarts)
_SESSION_KEY    = secrets.token_bytes(32)
_login_lock     = threading.Lock()
_login_attempts = {}
_LOGIN_MAX    = 10
_LOGIN_WINDOW = 60

def _make_session_token(password):
    return hmac.new(_SESSION_KEY, password.encode(), hashlib.sha256).hexdigest()

def _issue_session_token():
    return _make_session_token(PANEL_PASSWORD) if PANEL_PASSWORD else ""

def _check_session_token(tok):
    if not tok or not PANEL_PASSWORD: return False
    return hmac.compare_digest(tok, _make_session_token(PANEL_PASSWORD))

def _is_rate_limited(ip):
    now = time.time()
    with _login_lock:
        ts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW]
        if len(ts) >= _LOGIN_MAX:
            _login_attempts[ip] = ts
            return True
        ts.append(now)
        _login_attempts[ip] = ts
    return False


def get_codespace_cert_sha256():
    global _cached_cert_sha, _cached_cert_time
    if time.time() - _cached_cert_time < 3600 and _cached_cert_sha:
        return _cached_cert_sha
    if not PORT_DOMAIN:
        return ""
    try:
        hostname = PORT_DOMAIN
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((hostname, 443), timeout=3) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert_der = ssock.getpeercert(binary_form=True)
                h = hashlib.sha256(cert_der).digest()
                _cached_cert_sha = base64.b64encode(h).decode('utf-8')
                _cached_cert_time = time.time()
                return _cached_cert_sha
    except Exception:
        return _cached_cert_sha

SUB_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Subscription Profile</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><path fill='%2310b981' d='M165.9 397.4c0 2-2.3 3.6-5.2 3.6-3.3.3-5.6-1.3-5.6-3.6 0-2 2.3-3.6 5.2-3.6 3-.3 5.6 1.3 5.6 3.6zm-31.1-4.5c-.7 2 1.3 4.3 4.3 4.9 2.6 1 5.6 0 6.2-2s-1.3-4.3-4.3-5.2c-2.6-.7-5.5.3-6.2 2.3zm44.2-1.7c-2.9.7-4.9 2.6-4.6 4.9.3 2 2.9 3.3 5.9 2.6 2.9-.7 4.9-2.6 4.6-4.6-.3-1.9-3-3.2-5.9-2.9zM244.8 8C106.1 8 0 113.3 0 252c0 110.9 69.8 205.8 169.5 239.2 12.8 2.3 17.3-5.6 17.3-12.1 0-6.2-.3-40.4-.3-61.4 0 0-70 15-84.7-29.8 0 0-11.4-29.1-27.8-36.6 0 0-22.9-15.7 1.6-15.4 0 0 24.9 2 38.6 25.8 21.9 38.6 58.6 27.5 72.9 20.9 2.3-16 8.8-27.1 16-33.7-55.9-6.2-112.3-14.3-112.3-110.5 0-27.5 7.6-41.3 23.6-58.9-2.6-6.5-11.1-33.3 2.6-67.9 20.9-6.5 69 27 69 27 20-5.6 41.5-8.5 62.8-8.5s42.8 2.9 62.8 8.5c0 0 48.1-33.6 69-27 13.7 34.7 5.2 61.4 2.6 67.9 16 17.7 25.8 31.5 25.8 58.9 0 96.5-58.9 104.2-114.8 110.5 9.2 7.9 17 22.9 17 46.4 0 33.7-.3 75.4-.3 83.6 0 6.5 4.6 14.4 17.3 12.1C428.2 457.8 496 362.9 496 252 496 113.3 383.5 8 244.8 8zM97.2 352.9c-1.3 1-1 3.3.7 5.2 1.6 1.6 3.9 2.3 5.2 1 1.3-1 1-3.3-.7-5.2-1.6-1.6-3.9-2.3-5.2-1zm-10.8-8.1c-.7 1.3.3 2.9 2.3 3.9 1.6 1 3.6.7 4.3-.7.7-1.3-.3-2.9-2.3-3.9-2-.6-3.6-.3-4.3.7zm32.4 35.6c-1.6 1.3-1 4.3 1.3 6.2 2.3 2.3 5.2 2.6 6.5 1 1.3-1.3.7-4.3-1.3-6.2-2.2-2.3-5.2-2.6-6.5-1zm-11.4-14.7c-1.6 1-1.6 3.6 0 5.9 1.6 2.3 4.3 3.3 5.6 2.3 1.6-1.3 1.6-3.9 0-6.2-1.4-2.3-4-3.3-5.6-2z'/></svg>" />
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <style>
        :root { --bg-base: #09090b; --bg-panel: #121214; --bg-hover: #1f1f22; --border: rgba(255,255,255,0.08); --border-hover: rgba(255,255,255,0.15); --text-main: #fafafa; --text-muted: #a1a1aa; --accent: #10b981; --accent-hover: #059669; --accent-bg: rgba(16,185,129,0.12); --danger: #ef4444; --warning: #f59e0b; --success: #10b981; --info: #3b82f6; --purple: #8b5cf6; --radius-md: 16px; --radius-sm: 10px; }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; user-select: none; -webkit-user-select: none; }
        ::selection { background: rgba(16, 185, 129, 0.3); color: #fff; }
        input, textarea, select, .mono, pre, code, #log-output, td, .form-label, th, p { user-select: text !important; -webkit-user-select: text !important; }
        body { background: var(--bg-base); color: var(--text-main); font-family: 'Plus Jakarta Sans', sans-serif; margin: 0; padding: 24px 16px; display: flex; justify-content: center; min-height: 100vh; box-sizing: border-box; }
        .container { max-width: 480px; width: 100%; display: flex; flex-direction: column; gap: 20px; padding-bottom: 30px; }
        .card { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,0.4); }
        .card-title { margin: 0 0 16px 0; font-size: 1.15rem; font-weight: 800; display: flex; align-items: center; gap: 10px; }
        .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .stat-box { background: var(--bg-base); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); }
        .stat-label { font-size: 0.75rem; color: var(--text-muted); font-weight: 700; text-transform: uppercase; margin-bottom: 6px; letter-spacing: 0.05em; }
        .stat-val { font-size: 1.15rem; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
        .tag { padding: 4px 12px; border-radius: 8px; font-size: 0.7rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }
        .btn { width: 100%; background: var(--bg-hover); color: var(--text-main); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); font-size: 0.9rem; font-weight: 700; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; font-family: inherit; transition: all 0.2s ease; margin-top: 12px; }
        .btn:hover { background: var(--border-hover); transform: translateY(-1px); }
        .btn-primary { background: var(--accent); color: #000; border: none; box-shadow: 0 4px 12px rgba(16,185,129,0.3); }
        .btn-primary:hover { background: var(--accent-hover); color: #fff; }
        .btn-icon { width: 40px; height: 40px; padding: 0; margin: 0; }
        .link-item { background: var(--bg-base); border: 1px solid var(--border); padding: 14px; border-radius: var(--radius-sm); display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; transition: border-color 0.2s; }
        .link-item:hover { border-color: var(--border-hover); }
        .link-item-title { font-size: 0.9rem; font-weight: 700; margin-bottom: 4px; color: var(--text-main); }
        .link-item-sub { font-size: 0.75rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }
        .progress-bar { width: 100%; height: 8px; background: var(--bg-hover); border-radius: 4px; margin-top: 10px; overflow: hidden; }
        .progress-fill { height: 100%; background: var(--success); border-radius: 4px; transition: width 0.3s ease; }
        .progress-fill.warning { background: var(--warning); }
        .progress-fill.danger { background: var(--danger); }
        .qr-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(8px); justify-content: center; align-items: center; z-index: 100; padding: 20px; animation: fadeIn 0.2s ease; }
        .qr-modal.show { display: flex; }
        .qr-card { background: #fff; padding: 24px; border-radius: var(--radius-md); text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.5); transform: translateY(0); transition: transform 0.3s; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        
        .text-accent { color: var(--accent) !important; }
        .text-info { color: var(--info) !important; }
        .text-warning { color: var(--warning) !important; }
        .text-purple { color: var(--purple) !important; }
        
        .import-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 12px; }
        .btn-import { background: var(--bg-base); border: 1px solid var(--border); color: var(--text-main); text-decoration: none; padding: 14px 10px; border-radius: var(--radius-sm); font-size: 0.85rem; font-weight: 700; text-align: center; transition: all 0.2s; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; }
        .btn-import:hover { background: var(--bg-hover); border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 4px 12px rgba(16,185,129,0.15); }
        .btn-import i { font-size: 1.5rem; }
        
        .footer { text-align: center; margin-top: 20px; font-size: 0.8rem; color: var(--text-muted); font-weight: 600; }
        .footer a { color: var(--text-muted); text-decoration: none; transition: color 0.2s; }
        .footer a:hover { color: var(--text-main); }
    </style>
</head>
<body>
    <div class="container" id="app"></div>
    <div class="qr-modal" id="qr-modal" onclick="this.classList.remove('show')">
        <div class="qr-card" onclick="event.stopPropagation()">
            <div id="qrcode" style="display:inline-block; padding:10px; border:4px solid #f0f0f0; border-radius:12px; background:#fff;"></div>
            <button class="btn" style="margin-top:20px; background:#f4f4f5; color:#18181b; border:none;" onclick="document.getElementById('qr-modal').classList.remove('show')">Close QR</button>
        </div>
    </div>
    <script>
        const DATA = JSON.parse(atob('{{SUB_DATA_B64}}'));
        function fmtGB(v){ return !v ? '∞' : v.toFixed(2)+' GB'; }
        function fmtDate(d){ return !d ? 'Never' : new Date(d).toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'}); }
        function cp(t){ navigator.clipboard.writeText(t).then(()=>{ const el=document.createElement('div'); el.innerText='Copied!'; el.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--success);color:#fff;padding:10px 20px;border-radius:20px;font-weight:700;z-index:999;box-shadow:0 4px 12px rgba(16,185,129,0.3);'; document.body.appendChild(el); setTimeout(()=>el.remove(),2000); }); }
        function qr(t){ document.getElementById('qrcode').innerHTML=''; new QRCode(document.getElementById('qrcode'),{text:t,width:220,height:220,colorDark:"#000000",colorLight:"#ffffff",correctLevel:QRCode.CorrectLevel.M}); document.getElementById('qr-modal').classList.add('show'); }
        
        function render(){
            const u = DATA.client.usage||0; const l = DATA.client.limit||0; const p = l>0?Math.min(100,(u/l)*100):0;
            const cls = p>90?'danger':(p>75?'warning':'');
            const subUrl = encodeURIComponent(window.location.href);
            const subName = encodeURIComponent(DATA.client.name);
            const b64Url = btoa(window.location.href);
            
            document.getElementById('app').innerHTML = `
                <div style="text-align:center; margin-bottom:8px;">
                    <svg viewBox="0 0 496 512" fill="var(--accent)" style="width:52px; height:52px; margin-bottom:12px; filter:drop-shadow(0 0 12px var(--accent-bg));">
                        <path d="M165.9 397.4c0 2-2.3 3.6-5.2 3.6-3.3.3-5.6-1.3-5.6-3.6 0-2 2.3-3.6 5.2-3.6 3-.3 5.6 1.3 5.6 3.6zm-31.1-4.5c-.7 2 1.3 4.3 4.3 4.9 2.6 1 5.6 0 6.2-2s-1.3-4.3-4.3-5.2c-2.6-.7-5.5.3-6.2 2.3zm44.2-1.7c-2.9.7-4.9 2.6-4.6 4.9.3 2 2.9 3.3 5.9 2.6 2.9-.7 4.9-2.6 4.6-4.6-.3-1.9-3-3.2-5.9-2.9zM244.8 8C106.1 8 0 113.3 0 252c0 110.9 69.8 205.8 169.5 239.2 12.8 2.3 17.3-5.6 17.3-12.1 0-6.2-.3-40.4-.3-61.4 0 0-70 15-84.7-29.8 0 0-11.4-29.1-27.8-36.6 0 0-22.9-15.7 1.6-15.4 0 0 24.9 2 38.6 25.8 21.9 38.6 58.6 27.5 72.9 20.9 2.3-16 8.8-27.1 16-33.7-55.9-6.2-112.3-14.3-112.3-110.5 0-27.5 7.6-41.3 23.6-58.9-2.6-6.5-11.1-33.3 2.6-67.9 20.9-6.5 69 27 69 27 20-5.6 41.5-8.5 62.8-8.5s42.8 2.9 62.8 8.5c0 0 48.1-33.6 69-27 13.7 34.7 5.2 61.4 2.6 67.9 16 17.7 25.8 31.5 25.8 58.9 0 96.5-58.9 104.2-114.8 110.5 9.2 7.9 17 22.9 17 46.4 0 33.7-.3 75.4-.3 83.6 0 6.5 4.6 14.4 17.3 12.1C428.2 457.8 496 362.9 496 252 496 113.3 383.5 8 244.8 8zM97.2 352.9c-1.3 1-1 3.3.7 5.2 1.6 1.6 3.9 2.3 5.2 1 1.3-1 1-3.3-.7-5.2-1.6-1.6-3.9-2.3-5.2-1zm-10.8-8.1c-.7 1.3.3 2.9 2.3 3.9 1.6 1 3.6.7 4.3-.7.7-1.3-.3-2.9-2.3-3.9-2-.6-3.6-.3-4.3.7zm32.4 35.6c-1.6 1.3-1 4.3 1.3 6.2 2.3 2.3 5.2 2.6 6.5 1 1.3-1.3.7-4.3-1.3-6.2-2.2-2.3-5.2-2.6-6.5-1zm-11.4-14.7c-1.6 1-1.6 3.6 0 5.9 1.6 2.3 4.3 3.3 5.6 2.3 1.6-1.3 1.6-3.9 0-6.2-1.4-2.3-4-3.3-5.6-2z"/>
                    </svg>
                    <h1 style="margin:0; font-size:1.8rem; font-weight:800; letter-spacing:-0.03em;">G2Leafy</h1>
                    <p style="color:var(--text-muted); font-size:0.85rem; font-weight:600; margin-top:6px;">Subscription Environment</p>
                </div>
                
                <div class="card">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
                        <h2 class="card-title" style="margin:0;"><i class="fa-solid fa-user-shield text-accent"></i> ${DATA.client.name}</h2>
                        <span class="tag" style="background:${DATA.client.status?'var(--success)':'var(--danger)'}20; color:${DATA.client.status?'var(--success)':'var(--danger)'};">${DATA.client.status?'ACTIVE':'OFFLINE'}</span>
                    </div>
                    <div class="stat-grid">
                        <div class="stat-box"><div class="stat-label">Used Data</div><div class="stat-val">${u>0?u.toFixed(2):'0'} GB</div></div>
                        <div class="stat-box"><div class="stat-label">Total Quota</div><div class="stat-val">${fmtGB(l)}</div></div>
                        <div class="stat-box" style="grid-column:1/-1;">
                            <div style="display:flex; justify-content:space-between; align-items:center;"><span class="stat-label" style="margin:0;">Consumption</span><span style="font-size:0.8rem; font-weight:800;">${p.toFixed(1)}%</span></div>
                            <div class="progress-bar"><div class="progress-fill ${cls}" style="width:${p}%"></div></div>
                        </div>
                        <div class="stat-box"><div class="stat-label">Expiry</div><div class="stat-val" style="font-size:0.95rem;">${fmtDate(DATA.client.expiry)}</div></div>
                        <div class="stat-box"><div class="stat-label">Remaining</div><div class="stat-val" style="font-size:0.95rem;">${l?fmtGB(Math.max(0,l-u)):'∞'}</div></div>
                    </div>
                    <button class="btn btn-primary" style="margin-top:20px;" onclick="cp(window.location.href)"><i class="fa-solid fa-link"></i> Copy Subscription Link</button>
                    
                    <div style="margin-top:24px;">
                        <h3 style="font-size:0.9rem; font-weight:800; color:var(--text-main); margin:0 0 10px 0;"><i class="fa-solid fa-bolt text-warning"></i> One-Click Import</h3>
                        <div class="import-grid">
                            <a href="v2rayng://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192" width="26" height="26" style="color:var(--accent);"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="12" d="M22 39.005h40.738v113.99L170 39.005"/></svg> v2rayNG</a>
                            <a href="hiddify://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="26" height="26" style="color:var(--info);"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M33.578 19.376h8.146c.43 0 .776.346.776.777v19.785c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774V20.153c0-.43.346-.777.776-.777m8.146-12.091c.43 0 .776.347.776.777v8.359c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774v-3.769zM28.06 15.31c.43 0 .776.347.776.778v23.85c0 .43-.346.777-.776.777h-8.146a.775.775 0 0 1-.776-.774V20.68zm-13.638 8.15c.43 0 .776.347.776.778v15.7c0 .43-.346.777-.776.777H6.276a.775.775 0 0 1-.776-.777V28.83zm.777 11.419h3.94"/></svg> Hiddify</a>
                            <a href="shadowrocket://add/sub://${b64Url}?title=${subName}" class="btn-import"><i class="fa-solid fa-rocket text-warning"></i> Shadowrocket</a>
                            <a href="sing-box://import-remote-profile?url=${subUrl}&name=${subName}" class="btn-import"><i class="fa-solid fa-box text-purple"></i> Sing-Box</a>
                        </div>
                    </div>
                </div>
                
                <div class="card">
                    <h2 class="card-title"><i class="fa-solid fa-network-wired text-accent"></i> Core Configurations</h2>
                    <button class="btn" style="margin-bottom:20px; background:var(--accent-bg); color:var(--accent); border:none;" onclick="cp(DATA.links.join('\\n'))"><i class="fa-solid fa-copy"></i> Copy All Configs</button>
                    <div style="display:flex; flex-direction:column;">
                        ${DATA.links.map((lnk,i)=>{
                            let n = 'Node '+(i+1); try{n=decodeURIComponent(lnk.split('#')[1]||n);}catch(e){}
                            return `<div class="link-item">
                                <div style="min-width:0; flex:1; padding-right:16px;">
                                    <div class="link-item-title">${n}</div>
                                    <div class="link-item-sub">${lnk.substring(0,32)}...</div>
                                </div>
                                <div style="display:flex; gap:8px;">
                                    <button class="btn btn-icon" onclick="qr('${lnk}')"><i class="fa-solid fa-qrcode"></i></button>
                                    <button class="btn btn-icon" onclick="cp('${lnk}')"><i class="fa-solid fa-copy"></i></button>
                                </div>
                            </div>`;
                        }).join('')}
                    </div>
                </div>
                
                <div class="footer">
                    Powered by <a href="https://github.com/Code-Leafy/G2Leafy" target="_blank"><i class="fa-brands fa-github"></i> G2Leafy</a>
                </div>
            `;
        }
        render();
    </script>
</body>
</html>"""

# index.html is loaded from disk at request time -> see load_panel_html().

# panel-wiring.js is now inlined inside index.html (no separate file).

def log_sys_err(msg):
    try:
        with open(SYSTEM_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except: pass

# --- Frontend hot-loader ---------------------------------------------------
# Reads index.html from disk and keeps an mtime-cached copy. Because we check
# the file's modification time on every request, you can edit index.html while
# the server is running and the change shows up on the next page load - no
# restart, no rebuild. This is the "hot-swap frontend" hook.
_panel_html_cache = {"mtime": 0.0, "html": ""}

def load_panel_html():
    """Load index.html from disk (mtime-cached, hot-reloads on edit).

    Returns the RAW HTML with the {{PASS_SETUP}} / {{LOGGED_IN}} placeholders
    still intact - those are substituted per-request in the GET handler because
    LOGGED_IN depends on the caller's session cookie."""
    try:
        mtime = os.path.getmtime(INDEX_HTML_FILE)
        if mtime != _panel_html_cache["mtime"] or not _panel_html_cache["html"]:
            with open(INDEX_HTML_FILE, "r", encoding="utf-8") as f:
                _panel_html_cache["html"] = f.read()
            _panel_html_cache["mtime"] = mtime
        return _panel_html_cache["html"]
    except Exception as e:
        log_sys_err(f"Failed to load index.html: {e}")
        return _panel_html_cache["html"] or (
            "<!doctype html><meta charset=utf-8><title>G2Leafy</title>"
            "<body style=\"background:#09090b;color:#fafafa;font-family:sans-serif;"
            "padding:40px\"><h1>index.html not found</h1>"
            "<p>Place index.html next to backend.py and reload.</p>"
        )

def render_panel_html(logged_in):
    """Load index.html and substitute the runtime placeholders.

    PASS_SETUP is derived from the global PANEL_PASSWORD; LOGGED_IN is passed in
    because it depends on the current request's session cookie."""
    html = load_panel_html()
    html = html.replace("{{PASS_SETUP}}", "true" if PANEL_PASSWORD else "false")
    html = html.replace("{{LOGGED_IN}}", "true" if logged_in else "false")
    return html

def get_uuid():
    if not os.path.exists(UUID_FILE):
        try:
            with open(UUID_FILE, "w") as f: f.write(str(uuid.uuid4()))
        except Exception: pass
    try:
        with open(UUID_FILE) as f: return f.read().strip()
    except Exception: return str(uuid.uuid4())

def check_xray_running():
    try:
        out = subprocess.check_output(["pgrep", "-x", "xray"], text=True, stderr=subprocess.DEVNULL)
        return bool(out.strip())
    except Exception: return False

def check_port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1): return True
    except Exception: return False

def free_port(port):
    try:
        subprocess.run(f"sudo fuser -k -9 {port}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(f"sudo lsof -ti:{port} | xargs sudo kill -9", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

def full_cleanup():
    try: subprocess.run(["sudo", "pkill", "-9", "-x", "xray"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass
    free_port(XRAY_PORT)
    free_port(XRAY_XHTTP_PORT)
    free_port(XRAY_WS_PORT)
    free_port(WEB_PORT)
    free_port(API_PORT)
    time.sleep(0.5)

def count_client_connections():
    try:
        count = 0
        hex_port = f":{XRAY_PORT:04X}"
        for net_file in ['/proc/net/tcp', '/proc/net/tcp6']:
            try:
                with open(net_file, 'r') as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) > 3 and parts[1].endswith(hex_port) and parts[3] == '01': count += 1
            except Exception: pass
        return count
    except Exception: return 0

def make_port_public_via_api(port):
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not CODESPACE_NAME: return False
    url = f"https://api.github.com/user/codespaces/{CODESPACE_NAME}/ports/{port}"
    data = json.dumps({"visibility": "public"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PATCH")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e: 
        log_sys_err(f"REST API Port public failed for {port}: {e}")
        return False

def trigger_make_ports_public():
    global ports_thread_active
    with ports_thread_lock:
        if ports_thread_active: return
        ports_thread_active = True
    threading.Thread(target=_ports_worker, daemon=True).start()

def _ports_worker():
    global ports_thread_active
    time.sleep(5)
    for _ in range(12):
        made_xray = make_port_public_via_api(XRAY_PORT)
        made_web = make_port_public_via_api(WEB_PORT)
        if not made_xray:
            try: subprocess.run(f"gh codespace ports visibility {XRAY_PORT}:public -c {CODESPACE_NAME}", shell=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass
        if not made_web:
            try: subprocess.run(f"gh codespace ports visibility {WEB_PORT}:public -c {CODESPACE_NAME}", shell=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass
        time.sleep(5)
    with ports_thread_lock: ports_thread_active = False

def check_and_update():
    if not AUTO_UPDATE: return
    # The split ships as backend.py + index.html. The upstream repo only
    # publishes the single-file g2leafy.py, so comparing the two always differs
    # and the original auto-update would overwrite this split with the
    # single-file build (destroying the index.html / backend.py layout).
    # Only the original single-file build (g2leafy.py) self-updates.
    if os.path.basename(__file__) != "g2leafy.py":
        return
    try:
        req = urllib.request.urlopen(RAW_BASE + "g2leafy.py", timeout=5)
        remote_content = req.read()
        with open(__file__, "rb") as f: local_content = f.read()
        if remote_content.replace(b'\r\n', b'\n') != local_content.replace(b'\r\n', b'\n'):
            target = os.path.abspath(__file__)
            shutil.copyfile(target, target + ".bak")
            with open(target, "wb") as f: f.write(remote_content)
            os.chmod(target, 0o755)
            os.execv(sys.executable, [sys.executable, target])
    except Exception: pass

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def get_combined_state():
    try:
        with file_lock:
            with open(PANEL_STATE_FILE, "r") as f: panel_state = json.load(f)
    except Exception: panel_state = {}

    # Never echo panel password back to the browser.
    if isinstance(panel_state, dict):
        panel_state = json.loads(json.dumps(panel_state))
        _s = panel_state.get("settings")
        if isinstance(_s, dict):
            _s.pop("panelPassword", None)
            _s.pop("githubToken", None)  # legacy - remove if exists in old file

    logs = ""
    if os.path.exists(XRAY_LOG):
        try:
            with open(XRAY_LOG) as f: logs = "".join(f.readlines()[-20:])
        except: pass

    with state_lock:
        settings = panel_state.get("settings", {})
        # Quota Used = total Codespace/panel uptime, even if Xray is off
        uptime_h = state.get("uptime_sec", 0) / 3600.0
        quota_balance = float(settings.get("quotaBalance", 0))
        allowance_h = float(BASE_QUOTA_HOURS)
        funded_h = quota_balance * HOURS_PER_DOLLAR
        used_h = uptime_h
        total_h = allowance_h + funded_h
        quota_remain = max(0, total_h - used_h)

        usage_diffs = state.get("client_usage_bytes", {})
        for c in panel_state.get("clients", []):
            uuid_id = c.get("id")
            if uuid_id in usage_diffs:
                c["usage"] = c.get("usage", 0.0) + (usage_diffs[uuid_id] / 1073741824.0)

        telemetry = {
            "totalRxGb": state.get("total_down", 0) / 1073741824,
            "totalTxGb": state.get("total_up", 0) / 1073741824,
            "speedDownMbps": (state.get("speed_down_bps", 0) * 8) / 1000000.0,
            "speedUpMbps": (state.get("speed_up_bps", 0) * 8) / 1000000.0,
            "connections": state.get("conns", 0),
            "cpuPct": state.get("cpu_pct", 0),
            "ramMb": state.get("mem_used_mb", 0),
            "ramTotalMb": state.get("mem_total_mb", 4096),
            "diskUsedGb": state.get("disk_used_gb", 0),
            "diskTotalGb": state.get("disk_total_gb", 0),
            "loadAvg": state.get("load_avg", [0, 0, 0]),
            "xrayUptimeSec": state.get("xray_uptime_sec", 0),
            "xrayRunning": state.get("is_xray_running", False),
            "quotaTotalH": round(total_h, 1),
            "quotaUsedH": round(used_h, 1),
            "quotaRemainH": round(quota_remain, 1),
            "quotaAllowanceH": round(allowance_h, 1),
            "quotaFundedH": round(funded_h, 1),
            "buildId": BUILD_ID,
            "ipCity": state.get("ip_city", "N/A"),
            "ipCountry": state.get("ip_country", "N/A"),
            "ipIpv4": state.get("ip_ipv4", "N/A"),
            "certSha256": get_codespace_cert_sha256(),
            "githubUser": GITHUB_USER,
            "webDomain": WEB_DOMAIN,
            "tcpCc": _chosen_cc() or "default",
            "ipProvider": (state.get("ip_org", "") or "GitHub Codespaces"),
            "baseQuotaHours": BASE_QUOTA_HOURS,
            "hoursPerDollar": HOURS_PER_DOLLAR,
            "cpuCores": (os.cpu_count() or 2)
        }

    return json.dumps({
        "ok": True,
        "state": panel_state,
        "portDomain": PORT_DOMAIN,
        "logs": logs,
        **telemetry
    })

def save_panel_state(new_state):
    try:
        with file_lock:
            tmp = PANEL_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new_state, f, indent=2)
            os.rename(tmp, PANEL_STATE_FILE)
    except Exception: pass

def commit_client_usage():
    # Consistent lock order: file_lock -> state_lock to avoid deadlock
    # with do_PUT which also does file_lock -> state_lock
    with file_lock:
        with state_lock:
            usage_diffs = dict(state.get("client_usage_bytes", {}))
            if usage_diffs:
                state["client_usage_bytes"] = {}
            d = state["total_down"]
            u = state["total_up"]
            s = state["uptime_sec"]
            xs = state.get("xray_uptime_sec", 0)

        try:
            if not os.path.exists(PANEL_STATE_FILE): pstate = {}
            else:
                with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
            if usage_diffs:
                for c in pstate.get("clients", []):
                    uuid_id = c.get("id")
                    if uuid_id in usage_diffs:
                        c["usage"] = c.get("usage", 0.0) + (usage_diffs[uuid_id] / 1073741824.0)
            if "telemetry" not in pstate:
                pstate["telemetry"] = {}
            pstate["telemetry"]["total_down"] = d
            pstate["telemetry"]["total_up"] = u
            pstate["telemetry"]["uptime_sec"] = s
            pstate["telemetry"]["xray_uptime_sec"] = xs
            tmp = PANEL_STATE_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump(pstate, f, indent=2)
            os.rename(tmp, PANEL_STATE_FILE)
        except Exception:
            # Restore diffs if save failed
            if usage_diffs:
                with state_lock:
                    for k, v in usage_diffs.items():
                        state["client_usage_bytes"][k] = state["client_usage_bytes"].get(k, 0) + v

def format_vless_link(client_id, ip, port, client_name, transport="xhttp", path="/", mode="packet-up"):
    tag = urllib.parse.quote(client_name)
    addr = ip if ip else PORT_DOMAIN
    cert_hash = get_codespace_cert_sha256()
    cert_param = f"&cert={urllib.parse.quote(cert_hash)}" if cert_hash else ""
    if transport == "ws":
        return f"vless://{client_id}@{addr}:{port}?encryption=none&security=tls&sni={PORT_DOMAIN}&fp=chrome&alpn=h3,h2,http/1.1&type=ws&host={PORT_DOMAIN}&path=%2Fws{cert_param}#{tag}"
    else:
        return f"vless://{client_id}@{addr}:{port}?encryption=none&security=tls&sni={PORT_DOMAIN}&fp=chrome&alpn=h3,h2,http/1.1&type=xhttp&host={PORT_DOMAIN}&path={urllib.parse.quote(path)}{cert_param}&mode={mode}#{tag}"

def format_info_link(info_text):
    tag = urllib.parse.quote(info_text)
    return f"trojan://{get_uuid()}@127.0.0.1:80?security=none#{tag}"

def generate_sub_for_client(client_id):
    try:
        with file_lock:
            with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
    except Exception: return ""

    client = next((c for c in pstate.get("clients", []) if c.get("id") == client_id), None)
    if not client: return ""

    sub_map = pstate.get("subClientSubscriptions", {})
    client_sub = sub_map.get(client_id)

    with state_lock:
        uptime_h = state.get("uptime_sec", 0) / 3600
        settings = pstate.get("settings", {})
        q_bal = float(settings.get("quotaBalance", 0))
        q_tot = (q_bal * HOURS_PER_DOLLAR) + BASE_QUOTA_HOURS
        q_rem = max(0, q_tot - uptime_h)

    def apply_placeholders(text):
        if not text: return ""
        client_name = client.get("name", "")
        data_used_gb = client.get("usage", 0)
        data_total_gb = client.get("limit", 0)
        exp = client.get("expiry", "")
        
        text = text.replace("%client-name%", client_name)
        text = text.replace("%data-used%", f"{data_used_gb:.2f}")
        text = text.replace("%data-total%", f"{data_total_gb:.2f}" if data_total_gb else "∞")
        text = text.replace("%quota-used%", f"{uptime_h:.1f}")
        text = text.replace("%quota-remain%", f"{q_rem:.1f}")
        text = text.replace("%quota-total%", f"{q_tot:.1f}")
        text = text.replace("%expiry-date%", exp[:10] if exp else "Never")
        return text

    lines = []
    
    if client_sub and isinstance(client_sub, list) and len(client_sub) > 0:
        for entry in client_sub:
            if entry.get("type") == "proxy":
                name = apply_placeholders(entry.get("name", "Code-Leafy🍃 Auto"))
                ip = entry.get("ipAddress", "").strip()
                if not ip: ip = PORT_DOMAIN
                trans = entry.get("transport", "xhttp")
                lines.append(format_vless_link(client_id, ip, XRAY_PORT, name, trans, "/", "packet-up"))
            elif entry.get("type") == "info":
                name = apply_placeholders(entry.get("name", "Code-Leafy🍃 %data-used%GB / %data-total%GB | %quota-remain%h left"))
                lines.append(format_info_link(name))
    else:
        name = apply_placeholders(client.get("name", "G2Leafy_Client"))
        lines.append(format_vless_link(client_id, PORT_DOMAIN, XRAY_PORT, f"{name} (xHTTP)", "xhttp", "/", "packet-up"))
        lines.append(format_vless_link(client_id, PORT_DOMAIN, XRAY_PORT, f"{name} (WS)", "ws", "/", "packet-up"))

    return "\n".join(lines)

def generate_sub_link_url(client_id):
    token = base64.urlsafe_b64encode(client_id.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"https://{WEB_DOMAIN}/sub/{token}"

def _post_webhook(payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(DONATE_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")

def donate_heartbeat():
    if not DONATE_WEBHOOK_URL: return
    try:
        with file_lock:
            with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
        don_client = next((c for c in pstate.get("clients", []) if "Code-Leafy🍃 |" in c.get("name", "") or "Community_Donate" in c.get("name", "")), None)
        if not don_client: return
        cid = don_client["id"]
        tag = urllib.parse.quote(f"Code-Leafy🍃 | {GITHUB_USER}")
        cert_hash = get_codespace_cert_sha256()
        cert_param = f"&cert={urllib.parse.quote(cert_hash)}" if cert_hash else ""
        link = f"vless://{cid}@{DONATE_IP}:443?encryption=none&security=tls&sni={PORT_DOMAIN}&fp=chrome&alpn=h2,http/1.1&type=xhttp&host={PORT_DOMAIN}&path=%2F{cert_param}&mode=packet-up#{tag}"
        payload = {"action": "register", "id": f"{CODESPACE_NAME}"[:48] or get_uuid()[:12], "message": link, "label": GITHUB_USER[:64], "ttl": DONATE_TTL_SEC, "secret": DONATE_SECRET}
        _post_webhook(payload)
        with state_lock: state["donate_last"] = time.time()
    except Exception: pass

def donate_revoke():
    if not DONATE_WEBHOOK_URL: return
    try:
        payload = {"action": "revoke", "id": f"{CODESPACE_NAME}"[:48] or get_uuid()[:12], "secret": DONATE_SECRET}
        _post_webhook(payload)
    except Exception: pass

def handle_api_action(data):
    action = data.get("action")
    if action == "start": start_xray()
    elif action == "stop": stop_xray()
    elif action == "restart": start_xray()
    elif action == "clear_logs":
        try: open(XRAY_LOG, "w").close()
        except Exception: pass

def get_session_cookie(headers):
    for c in headers.get('Cookie', '').split(';'):
        c = c.strip()
        if c.startswith('sess='):
            return urllib.parse.unquote(c[5:])
    return ""

class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    
    def check_auth(self):
        if not PANEL_PASSWORD: return False
        return _check_session_token(get_session_cookie(self.headers))

    def send_json(self, status, payload):
        try:
            self.send_response(status)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        except: pass

    def do_GET(self):
        try:
            parsed_path = urllib.parse.urlparse(self.path)
            base_path = parsed_path.path
            
            if base_path.startswith('http://') or base_path.startswith('https://'):
                base_path = urllib.parse.urlparse(self.path).path

            if not base_path:
                base_path = '/'
            
            if base_path.startswith('/sub/'):
                token = base_path.split('/')[-1]
                token += "=" * ((4 - len(token) % 4) % 4)
                try:
                    client_id = base64.urlsafe_b64decode(token).decode("utf-8")
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return
                
                ua = self.headers.get("User-Agent", "").lower()
                is_client = any(x in ua for x in ["v2ray", "clash", "neko", "sing-box", "go-http", "shadowrocket", "surge", "quantumult", "xray"])
                is_browser = not is_client and any(x in ua for x in ["mozilla", "chrome", "safari", "applewebkit", "edge"])
                
                with file_lock:
                    try:
                        with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
                    except Exception: pstate = {}
                    
                client = next((c for c in pstate.get("clients", []) if c.get("id") == client_id), None)
                if not client:
                    self.send_response(404)
                    self.end_headers()
                    return

                sub_content = generate_sub_for_client(client_id)
                
                if is_browser:
                    with state_lock:
                        usage_diffs = state.get("client_usage_bytes", {})
                        if client_id in usage_diffs:
                            client["usage"] = client.get("usage", 0.0) + (usage_diffs[client_id] / 1073741824.0)

                    sub_data = {
                        "client": {
                            "name": client.get("name", ""),
                            "usage": client.get("usage", 0.0),
                            "limit": client.get("limit", 0.0),
                            "expiry": client.get("expiry", ""),
                            "status": client.get("status", 1)
                        },
                        "links": sub_content.split('\n') if sub_content else []
                    }
                    
                    b64_json = base64.b64encode(json.dumps(sub_data).encode("utf-8")).decode("utf-8")
                    html = SUB_HTML_TEMPLATE.replace("{{SUB_DATA_B64}}", b64_json)
                    
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(html.encode("utf-8"))
                else:
                    b64_content = base64.b64encode(sub_content.encode("utf-8")).decode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                    self.end_headers()
                    self.wfile.write(b64_content.encode("utf-8"))
                return

            if base_path in ('/', '', '/panel', '/panel/', '/login', '/login/', '/admin', '/admin/'):
                html = render_panel_html(self.check_auth())
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))
                return

            if not base_path.startswith('/api/'):
                self.send_json(404, {"ok": False, "error": "Not Found"})
                return

            if not self.check_auth():
                self.send_json(401, {"ok": False, "error": "Unauthorized"})
                return

            if base_path == '/api/state':
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(get_combined_state().encode('utf-8'))
            elif base_path.startswith('/api/sub/link/'):
                client_id = base_path.split('/')[-1]
                link = generate_sub_link_url(urllib.parse.unquote(client_id))
                self.send_json(200, {"ok": True, "link": link})
            elif base_path == '/api/config':
                generate_xray_config()
                try:
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                except Exception:
                    cfg = {}
                self.send_json(200, {"ok": True, "config": cfg})
            else:
                self.send_json(404, {"ok": False, "error": "Not Found"})
        except Exception as e: 
            log_sys_err(f"GET exception: {e}")
            self.send_json(500, {"ok": False, "error": str(e)})
        
    def do_PUT(self):
        try:
            if not self.check_auth():
                self.send_json(401, {"ok": False, "error": "Unauthorized"})
                return

            parsed = urllib.parse.urlparse(self.path)
            base_path = parsed.path
            if not base_path: base_path = '/'
            
            if base_path == '/api/state':
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > 5_000_000:
                    self.send_json(400, {"ok": False, "error": "Invalid Content-Length"})
                    return
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)
                new_state = data.get("state", {})
                
                with file_lock:
                    try:
                        with open(PANEL_STATE_FILE, "r") as f: old_pstate = json.load(f)
                    except Exception: old_pstate = {}
                    
                    old_usages = {c["id"]: c.get("usage", 0.0) for c in old_pstate.get("clients", [])}
                    with state_lock:
                        for cid, diff in state.get("client_usage_bytes", {}).items():
                            old_usages[cid] = old_usages.get(cid, 0.0) + (diff / 1073741824.0)
                        state["client_usage_bytes"] = {}
                        
                    for c in new_state.get("clients", []):
                        if c["id"] in old_usages:
                            c["usage"] = old_usages[c["id"]]
                            
                    with state_lock:
                        # Merge: keep file's telemetry as base but always use live
                        # in-memory counters (down/up/uptime) so quota doesn't go
                        # backwards when a PUT happens between commits
                        base_tel = old_pstate.get("telemetry", {})
                        if not base_tel:
                            base_tel = {
                                "total_down": state.get("total_down", 0),
                                "total_up": state.get("total_up", 0),
                                "uptime_sec": state.get("uptime_sec", 0),
                                "xray_uptime_sec": state.get("xray_uptime_sec", 0)
                            }
                        else:
                            # Update live counters from memory
                            base_tel["total_down"] = state.get("total_down", base_tel.get("total_down", 0))
                            base_tel["total_up"] = state.get("total_up", base_tel.get("total_up", 0))
                            base_tel["uptime_sec"] = state.get("uptime_sec", base_tel.get("uptime_sec", 0))
                            base_tel["xray_uptime_sec"] = state.get("xray_uptime_sec", base_tel.get("xray_uptime_sec", base_tel.get("uptime_sec", 0)))
                        new_state["telemetry"] = base_tel
                    
                    if "settings" not in new_state: new_state["settings"] = {}
                    if old_pstate.get("settings", {}).get("panelPassword"):
                        new_state["settings"]["panelPassword"] = old_pstate["settings"]["panelPassword"]
                    # legacy githubToken - explicitly drop if present in new_state
                    if "githubToken" in new_state.get("settings", {}):
                        new_state["settings"].pop("githubToken", None)

                    try:
                        tmp = PANEL_STATE_FILE + ".tmp"
                        with open(tmp, "w") as f: json.dump(new_state, f, indent=2)
                        os.rename(tmp, PANEL_STATE_FILE)
                    except Exception as fe:
                        log_sys_err(f"File save error: {fe}")
                
                reason = data.get("reason", "")
                if reason in ["saveClient", "deleteClient", "saveAdvancedRules", "import"]:
                    generate_xray_config()
                    threading.Thread(target=lambda: (stop_xray(), time.sleep(0.5), start_xray())).start()

                self.send_json(200, {"ok": True})
            else:
                self.send_json(404, {"ok": False, "error": "Not Found"})
        except Exception as e: 
            log_sys_err(f"PUT exception: {e}")
            self.send_json(500, {"ok": False, "error": str(e)})
        
    def do_POST(self):
        global PANEL_PASSWORD
        try:
            parsed = urllib.parse.urlparse(self.path)
            base_path = parsed.path
            if not base_path: base_path = '/'
            
            if base_path == '/api/login':
                ip = self.client_address[0]
                if _is_rate_limited(ip):
                    self.send_json(429, {"ok": False, "error": "Too many attempts"})
                    return
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > 5_000_000:
                    self.send_json(400, {"ok": False, "error": "Invalid Content-Length"})
                    return
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                supplied = data.get("pass", "")
                if PANEL_PASSWORD and hmac.compare_digest(supplied, PANEL_PASSWORD):
                    self.send_response(200)
                    self.send_header('Set-Cookie', f'sess={urllib.parse.quote(_issue_session_token())}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000')
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_json(401, {"ok": False})
                return
                
            if base_path == '/api/setup':
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > 5_000_000:
                    self.send_json(400, {"ok": False, "error": "Invalid Content-Length"})
                    return
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                new_pass = data.get("pass", "")
                if not PANEL_PASSWORD and new_pass:
                    PANEL_PASSWORD = new_pass
                    with file_lock:
                        try:
                            with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
                        except Exception: pstate = {}
                        if "settings" not in pstate: pstate["settings"] = {}
                        pstate["settings"]["panelPassword"] = PANEL_PASSWORD
                        tmp = PANEL_STATE_FILE + ".tmp"
                        with open(tmp, "w") as f: json.dump(pstate, f, indent=2)
                        os.rename(tmp, PANEL_STATE_FILE)
                        
                    self.send_response(200)
                    self.send_header('Set-Cookie', f'sess={urllib.parse.quote(_issue_session_token())}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000')
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_json(400, {"ok": False})
                return
                
            if not self.check_auth():
                self.send_json(401, {"ok": False, "error": "Unauthorized"})
                return

            if base_path == '/api/action':
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > 5_000_000:
                    self.send_json(400, {"ok": False, "error": "Invalid Content-Length"})
                    return
                body = self.rfile.read(length).decode('utf-8')
                data = json.loads(body)
                handle_api_action(data)
                self.send_json(200, {"ok": True})
            elif base_path == '/api/donate':
                with state_lock: state["donate_active"] = True
                threading.Thread(target=donate_heartbeat, daemon=True).start()
                self.send_json(200, {"ok": True, "message": "Donated via API", "donated": True})
            elif base_path == '/api/backup':
                backup_name = f"panel_state_backup_{int(time.time())}.json"
                if os.path.exists(PANEL_STATE_FILE):
                    with file_lock:
                        shutil.copyfile(PANEL_STATE_FILE, os.path.join(DATA_DIR, backup_name))
                self.send_json(200, {"ok": True, "file": backup_name})
            else:
                self.send_json(404, {"ok": False, "error": "Not Found"})
        except Exception as e:
            log_sys_err(f"POST exception: {e}")
            self.send_json(500, {"ok": False, "error": str(e)})

def web_server_thread(port):
    while engine_running:
        try: 
            server = ThreadedHTTPServer(('0.0.0.0', port), WebUIHandler)
            server.serve_forever()
        except Exception as e: 
            log_sys_err(f"Web server failed on port {port}: {e}")
            time.sleep(2)

async def multiplexer(reader, writer):
    try:
        data = await reader.read(4096)
        if not data:
            writer.close()
            return

        target_port = XRAY_XHTTP_PORT
        if b" /ws" in data:
            target_port = XRAY_WS_PORT

        t_reader, t_writer = await asyncio.open_connection('127.0.0.1', target_port)
        t_writer.write(data)
        await t_writer.drain()

        async def pipe(r, w):
            try:
                while True:
                    d = await r.read(65536)
                    if not d: break
                    w.write(d)
                    await w.drain()
            except: pass
            finally:
                try: w.close()
                except: pass

        asyncio.create_task(pipe(reader, t_writer))
        asyncio.create_task(pipe(t_reader, writer))
    except Exception:
        try: writer.close()
        except: pass

def start_multiplexer():
    global _mux_started
    with _mux_lock:
        if _mux_started:
            return
        _mux_started = True
    
    def run():
        global _mux_started
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            server = loop.run_until_complete(asyncio.start_server(multiplexer, '0.0.0.0', XRAY_PORT))
            loop.run_forever()
        except Exception as e:
            log_sys_err(f"Multiplexer error: {e}")
            with _mux_lock:
                _mux_started = False
    threading.Thread(target=run, daemon=True).start()

last_cpu_idle = 0.0
last_cpu_total = 0.0
_cpu_primed = False

def sample_cpu_pct():
    global last_cpu_idle, last_cpu_total, _cpu_primed
    try:
        with open('/proc/stat') as f: line = f.readline()
        fields = [float(column) for column in line.strip().split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields)
        
        if not _cpu_primed:
            last_cpu_idle, last_cpu_total = idle, total
            _cpu_primed = True
            return 0.0
        
        idle_delta = idle - last_cpu_idle
        total_delta = total - last_cpu_total
        last_cpu_idle, last_cpu_total = idle, total
        if total_delta <= 0: return 0.0
        return min(100.0, max(0.0, 100.0 * (1.0 - idle_delta / total_delta)))
    except Exception: return 0.0

def fetch_ip_info():
    try:
        req = urllib.request.urlopen("https://ipinfo.io/json", timeout=10)
        data = json.loads(req.read().decode())
        with state_lock:
            state["ip_city"] = data.get("city", "Unknown")
            state["ip_country"] = data.get("country", "Unknown")
            state["ip_ipv4"] = data.get("ip", "Unknown")
            state["ip_org"] = data.get("org", "")
    except Exception: pass

def system_monitor_thread():
    global state

    tick = 0
    while engine_running:
        tick += 1
        try:
            cpu_val = sample_cpu_pct()
            try: la = list(os.getloadavg())
            except Exception: la = [0,0,0]
            
            used = 0
            tot = 0
            try:
                with open('/proc/meminfo') as f:
                    mem = {}
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2: mem[parts[0].strip(':')] = int(parts[1])
                used = (mem.get('MemTotal', 0) - mem.get('MemAvailable', mem.get('MemFree', 0))) / 1024
                tot = mem.get('MemTotal', 0) / 1024
            except Exception: pass
            
            try:
                total_d, used_d, free_d = shutil.disk_usage("/")
                disk_used_gb = used_d / 1073741824.0
                disk_total_gb = total_d / 1073741824.0
            except Exception:
                disk_used_gb = disk_total_gb = 0

            with state_lock:
                state["cpu_pct"] = cpu_val
                state["mem_used_mb"] = used
                state["mem_total_mb"] = tot
                state["disk_used_gb"] = disk_used_gb
                state["disk_total_gb"] = disk_total_gb
                state["load_avg"] = la
                # Quota Used = total panel/Codespace uptime, counts even when Xray is off
                state["uptime_sec"] = state.get("uptime_sec", 0) + 1.0

            if tick % 10 == 0:
                commit_client_usage()
                
        except Exception: pass

        time.sleep(1)

def xray_monitor_thread():
    global state
    last_fd = None
    last_fu = None
    last_user_stats = {}
    last_stats_time = time.time()

    tick = 0
    while engine_running:
        tick += 1
        is_running = check_xray_running()
        with state_lock: state["is_xray_running"] = is_running

        if tick > 10 and tick % 30 == 0:
            if not is_running or not check_port_listening(XRAY_XHTTP_PORT):
                start_xray()
                with state_lock: state["is_xray_running"] = check_xray_running()

        if tick % 120 == 0 and is_running:
            trigger_make_ports_public()

        # Update connections count every few ticks
        if tick % 5 == 0:
            with state_lock:
                state["conns"] = count_client_connections()

        if is_running:
            now = time.time()
            elapsed = now - last_stats_time
            if elapsed <= 0: elapsed = 1.0
            if elapsed > 10: elapsed = 1.0
            last_stats_time = now
            with state_lock:
                state["xray_uptime_sec"] = state.get("xray_uptime_sec", 0) + elapsed
            try:
                out = subprocess.check_output(["timeout", "2", XRAY_BIN, "api", "statsquery", f"-server=127.0.0.1:{API_PORT}"], text=True, stderr=subprocess.DEVNULL)
                stats = []
                try:
                    data = json.loads(out)
                    stats = data.get("stat", []) or []
                except Exception:
                    for m in re.finditer(r'name:\s*"([^"]+)".*?value:\s*(\d+)', out, re.S):
                        stats.append({"name": m.group(1), "value": int(m.group(2))})

                fd = fu = 0
                user_usage_diffs = {}

                for s in stats:
                    name = s.get("name", "")
                    try: val = int(s.get("value", 0))
                    except Exception: val = 0

                    parts = name.split(">>>")
                    if len(parts) == 4:
                        if parts[0] == "inbound" and parts[1] != "api":
                            if parts[3] == "downlink": fd += val
                            elif parts[3] == "uplink": fu += val
                        elif parts[0] == "user":
                            email_uuid = parts[1]
                            key = f"{email_uuid}_{parts[3]}"
                            prev = last_user_stats.get(key, val)
                            delta = val - prev if val >= prev else val
                            last_user_stats[key] = val
                            if delta > 0:
                                user_usage_diffs[email_uuid] = user_usage_diffs.get(email_uuid, 0) + delta

                dt_down = (fd - last_fd) if (last_fd is not None and fd >= last_fd) else fd
                dt_up = (fu - last_fu) if (last_fu is not None and fu >= last_fu) else fu
                last_fd = fd
                last_fu = fu

                # Calculate actual speed based on elapsed time
                actual_speed_down = dt_down / elapsed if elapsed > 0 else 0
                actual_speed_up = dt_up / elapsed if elapsed > 0 else 0

                with state_lock:
                    state["total_down"] += dt_down
                    state["total_up"] += dt_up
                    state["speed_down_bps"] = actual_speed_down
                    state["speed_up_bps"] = actual_speed_up

                    if user_usage_diffs:
                        if "client_usage_bytes" not in state: state["client_usage_bytes"] = {}
                        for email_uuid, diff in user_usage_diffs.items():
                            state["client_usage_bytes"][email_uuid] = state["client_usage_bytes"].get(email_uuid, 0) + diff
            except Exception:
                with state_lock:
                    state["speed_down_bps"] = 0
                    state["speed_up_bps"] = 0
        else:
            last_stats_time = time.time()
            with state_lock:
                state["speed_down_bps"] = 0
                state["speed_up_bps"] = 0

        with state_lock:
            don_active = state.get("donate_active", False)
            don_last = state.get("donate_last", 0)
            u_sec = state.get("uptime_sec", 0)
            is_running_snap = state.get("is_xray_running", False)

        if don_active:
            with file_lock:
                try:
                    with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
                    quota_balance = float(pstate.get("settings", {}).get("quotaBalance", 0))
                    quota_total_h = (quota_balance * HOURS_PER_DOLLAR) + BASE_QUOTA_HOURS
                except Exception:
                    quota_total_h = 60

            left = quota_total_h * 3600 - u_sec
            if not is_running_snap or left <= DONATE_QUOTA_GRACE_SEC:
                donate_revoke()
                with state_lock: state["donate_active"] = False
            elif (time.time() - don_last) >= DONATE_HEARTBEAT_SEC:
                threading.Thread(target=donate_heartbeat, daemon=True).start()

        time.sleep(1)

# --- TCP acceleration (BBR) -------------------------------------------------
# "Super-charge TCP": BBR keeps throughput high on lossy / long-distance links
# (the common case for this kind of proxy). Everything here is best-effort: if
# the host kernel can't provide BBR we fall back to the kernel default, so the
# generated config never references an algorithm the system can't deliver and
# Xray never fails to start because of it.
_tcp_cc_cache = {"cc": None}

def _chosen_cc():
    """Return 'bbr' if available on this host, else None (use kernel default)."""
    if _tcp_cc_cache["cc"]:
        return _tcp_cc_cache["cc"]
    try:
        avail = open("/proc/sys/net/ipv4/tcp_available_congestion_control").read().split()
    except Exception:
        avail = []
    _tcp_cc_cache["cc"] = "bbr" if "bbr" in avail else None
    return _tcp_cc_cache["cc"]

def _tcp_sockopt():
    """Optimized socket options applied to every inbound.
    tcpFastOpen/tcpNoDelay cut handshake + latency, keepalives drop dead
    sockets fast, and tcpCongestion uses BBR when present."""
    s = {"tcpFastOpen": True, "tcpNoDelay": True,
         "tcpKeepAliveIdle": 50, "tcpKeepAliveInterval": 15}
    cc = _chosen_cc()
    if cc:
        s["tcpCongestion"] = cc
    return s

def _direct_outbound(domain_strategy="UseIP"):
    """freedom outbound with the same TCP acceleration applied to the
    server -> destination leg (not just client -> server)."""
    ob = {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": domain_strategy}}
    so = {"tcpFastOpen": True, "tcpNoDelay": True}
    cc = _chosen_cc()
    if cc:
        so["tcpCongestion"] = cc
    ob["streamSettings"] = {"sockopt": so}
    return ob

def generate_xray_config():
    try:
        with file_lock:
            with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
    except Exception: pstate = {}

    clients_data = pstate.get("clients", [])
    settings = pstate.get("settings", {})
    adv = settings.get("advanced", {})

    rules = [{"inboundTag": ["api"], "outboundTag": "api", "type": "field"}]

    inb_clients = []
    seen_ids = set()
    for c in clients_data:
        cid = str(c.get("id", "")).strip()
        if c.get("status", 1) == 1 and cid not in seen_ids:
            if re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', cid):
                seen_ids.add(cid)
                inb_clients.append({"id": cid, "level": 0, "email": cid})
    
    if not inb_clients: 
        inb_clients.append({"id": get_uuid(), "email": "dummy"})
        
    sniff_override = []
    if adv.get("sniffHttp", True): sniff_override.append("http")
    if adv.get("sniffTls", True): sniff_override.append("tls")
    if adv.get("sniffQuic", True): sniff_override.append("quic")
    if adv.get("sniffFakedns", False): sniff_override.append("fakedns")

    inbounds = [
        {
            "tag": "vless-xhttp", "port": XRAY_XHTTP_PORT, "listen": "127.0.0.1", "protocol": "vless",
            "settings": { "clients": inb_clients, "decryption": "none" },
            "streamSettings": {
                "network": "xhttp", "security": "none",
                "xhttpSettings": { "mode": "packet-up", "path": "/" },
                "sockopt": _tcp_sockopt()
            },
            "sniffing": { "enabled": adv.get("deepSniff", True), "destOverride": sniff_override }
        },
        {
            "tag": "vless-ws", "port": XRAY_WS_PORT, "listen": "127.0.0.1", "protocol": "vless",
            "settings": { "clients": inb_clients, "decryption": "none" },
            "streamSettings": {
                "network": "ws", "security": "none",
                "wsSettings": { "path": "/ws" },
                "sockopt": _tcp_sockopt()
            },
            "sniffing": { "enabled": adv.get("deepSniff", True), "destOverride": sniff_override }
        },
        {
            "listen": "127.0.0.1", "port": API_PORT, "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"}, "tag": "api"
        }
    ]

    if adv.get("bypassIr", False): rules.append({"domain": ["geosite:ir"], "ip": ["geoip:ir"], "outboundTag": "direct", "type": "field"})
    if adv.get("bypassRu", False): rules.append({"domain": ["geosite:ru"], "ip": ["geoip:ru"], "outboundTag": "direct", "type": "field"})
    if adv.get("bypassCn", False): rules.append({"domain": ["geosite:cn"], "ip": ["geoip:cn"], "outboundTag": "direct", "type": "field"})
    if adv.get("bypassLan", False): rules.append({"ip": ["geoip:private"], "outboundTag": "direct", "type": "field"})

    cfg = {
        "log": {
            "loglevel": adv.get("logLevel", "warning"),
            "access": XRAY_ACCESS_LOG if adv.get("accessLog", False) else "none",
            "error": XRAY_LOG
        },
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "dns": {
            "servers": [adv.get("dnsPrimary", "1.1.1.1"), adv.get("dnsFallback", "8.8.8.8")],
            "disableCache": not adv.get("dnsCache", True)
        },
        "routing": { "rules": rules },
        "policy": {
            "system": {"statsInboundDownlink": True, "statsInboundUplink": True},
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "bufferSize": 4, "connIdle": 300, "handshake": 4}}
        },
        "inbounds": inbounds,
        "outbounds": [
            _direct_outbound(adv.get("domainStrategy", "UseIP")),
            {"tag": "block", "protocol": "blackhole"}
        ]
    }

    if adv.get("mux", False):
        cfg["outbounds"][0]["mux"] = { "enabled": True, "concurrency": adv.get("muxConcurrency", 8) }
    
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(cfg, f, indent=2)
        os.rename(tmp, CONFIG_FILE)
    except Exception: pass

def start_xray():
    with _xray_start_lock:
        try: subprocess.run(f"setcap cap_net_bind_service=+ep {XRAY_BIN}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: pass
        try: subprocess.run(f"sudo setcap cap_net_bind_service=+ep {XRAY_BIN}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: pass
        try: subprocess.run("sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception: pass

        # Best-effort TCP acceleration: load BBR + fq queueing discipline, then
        # refresh the cached congestion-control detection so the generated
        # config picks it up. Any failure is harmless (Xray falls back to the
        # kernel default).
        for _cmd in ("sudo modprobe tcp_bbr",
                     "sudo sysctl -w net.core.default_qdisc=fq",
                     "sudo sysctl -w net.ipv4.tcp_congestion_control=bbr",
                     "sudo sysctl -w net.ipv4.tcp_notsent_lowat=16384"):
            try: subprocess.run(_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass
        _tcp_cc_cache["cc"] = None
        _chosen_cc()

        for attempt in range(5):
            full_cleanup()
            generate_xray_config()
            
            try:
                subprocess.check_output([XRAY_BIN, "run", "-test", "-c", CONFIG_FILE], stderr=subprocess.STDOUT, text=True)
            except subprocess.CalledProcessError as e:
                log_sys_err(f"xray config test failed (attempt {attempt+1}): {e.output}")
                stop_xray()
                time.sleep(1.5)
                continue

            try: subprocess.Popen([XRAY_BIN, "run", "-c", CONFIG_FILE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass
                
            ok = False
            for _ in range(75):
                if check_xray_running() and check_port_listening(XRAY_XHTTP_PORT):
                    ok = True
                    break
                time.sleep(0.2)
            if ok:
                start_multiplexer()
                trigger_make_ports_public()
                return
            stop_xray()
            time.sleep(1.5)

def stop_xray():
    try: subprocess.run(["sudo", "pkill", "-9", "-x", "xray"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

def print_start_banner():
    panel_url = f"https://{WEB_DOMAIN}/"
    print("\n" + "="*60)
    print("🚀 G2LEAFY STARTED SUCCESSFULLY")
    print("="*60)
    print(f"🌐 Access Web Panel & Subscriptions: \033[92m\033[4m{panel_url}\033[0m")
    print(f"🔗 Forwarded Xray Port: \033[94m{PORT_DOMAIN}:{XRAY_PORT}\033[0m")
    print("="*60 + "\n")

def handle_exit(signum, frame):
    global engine_running
    engine_running = False
    commit_client_usage()
    if state.get("donate_active"):
        try: donate_revoke()
        except Exception: pass
    sys.exit(0)

def main():
    global engine_running, PANEL_PASSWORD
    
    signal.signal(signal.SIGTERM, handle_exit)
    signal.signal(signal.SIGINT, handle_exit)

    check_and_update()
    full_cleanup()
    
    if not os.path.exists(PANEL_STATE_FILE):
        with file_lock:
            with open(PANEL_STATE_FILE, "w") as f:
                json.dump({
                    "clients": [],
                    "settings": {
                        "panelPassword": "",
                        "advanced": {"logLevel": "warning", "domainStrategy": "UseIP", "dnsPrimary": "1.1.1.1", "dnsFallback": "8.8.8.8"}
                    },
                    "telemetry": {"total_down": 0, "total_up": 0, "uptime_sec": 0}
                }, f)

    try:
        with file_lock:
            with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
        tel = pstate.get("telemetry", {})
        state["total_down"] = tel.get("total_down", 0)
        state["total_up"] = tel.get("total_up", 0)
        state["uptime_sec"] = tel.get("uptime_sec", 0)
        state["xray_uptime_sec"] = tel.get("xray_uptime_sec", tel.get("uptime_sec", 0))

        saved_pass = pstate.get("settings", {}).get("panelPassword", "")
        if saved_pass and not PANEL_PASSWORD:
            PANEL_PASSWORD = saved_pass

        if any("Community_Donate" in c.get("name", "") or "Code-Leafy🍃 |" in c.get("name", "") for c in pstate.get("clients", [])):
            state["donate_active"] = True
    except Exception: pass

    start_xray()

    threading.Thread(target=fetch_ip_info, daemon=True).start()
    threading.Thread(target=system_monitor_thread, daemon=True).start()
    threading.Thread(target=xray_monitor_thread, daemon=True).start()
    threading.Thread(target=web_server_thread, args=(WEB_PORT,), daemon=True).start()
    
    time.sleep(2)
    print_start_banner()
    
    try:
        while True: time.sleep(10)
    except KeyboardInterrupt: pass
    finally:
        engine_running = False
        commit_client_usage()
        if state.get("donate_active"):
            try: donate_revoke()
            except Exception: pass

if __name__ == "__main__":
    main()
