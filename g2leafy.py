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
import webbrowser
import signal
import ssl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

LOCAL_VERSION = "4.0.0"
AUTO_UPDATE = True
UPSTREAM_REPO = "Code-Leafy/G2ray"
RAW_BASE = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/refs/heads/main/"

DONATE_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbxbTxcCS6sl7HpASqssmr6c9wYL1gsE86fBjFHTcRs0sl0o-R5ZAmKJk-z_GaBBRqcsHw/exec"
DONATE_SECRET = ""
DONATE_IP = "20.120.56.11"
DONATE_HEARTBEAT_SEC = 240
DONATE_TTL_SEC = 720
DONATE_QUOTA_GRACE_SEC = 600

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

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

for d in [DATA_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

state_lock = threading.Lock()
file_lock = threading.RLock()
engine_running = True
ports_thread_active = False
ports_thread_lock = threading.Lock()

state = {
    "total_down": 0, "total_up": 0, "uptime_sec": 0,
    "speed_down_bps": 0, "speed_up_bps": 0, "cpu_pct": 0.0,
    "mem_used_mb": 0, "mem_total_mb": 4096,
    "disk_used_gb": 0, "disk_total_gb": 0,
    "load_avg": [0,0,0], "is_xray_running": False,
    "client_usage_bytes": {},
    "ip_city": "N/A", "ip_country": "N/A", "ip_ipv4": "N/A",
    "donate_active": False, "donate_last": 0
}

try:
    CODESPACE_NAME = os.environ.get("CODESPACE_NAME")
    if not CODESPACE_NAME:
        CODESPACE_NAME = subprocess.check_output(["gh", "codespace", "list", "--limit", "1", "--json", "name", "--jq", ".[0].name"], text=True, stderr=subprocess.DEVNULL).strip()
except Exception:
    CODESPACE_NAME = os.uname().nodename

PORT_DOMAIN = f"{CODESPACE_NAME}-{XRAY_PORT}.app.github.dev"
WEB_DOMAIN = f"{CODESPACE_NAME}-{WEB_PORT}.app.github.dev"
GITHUB_USER = os.environ.get("GITHUB_USER", CODESPACE_NAME.split('-')[0] if '-' in CODESPACE_NAME else "User")
PANEL_PASSWORD = os.environ.get("PASS", "")

_cached_cert_sha = ""
_cached_cert_time = 0

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
                    <h1 style="margin:0; font-size:1.8rem; font-weight:800; letter-spacing:-0.03em;">G2ray Panel</h1>
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
                            <a href="v2rayng://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><i class="fa-solid fa-v text-accent"></i> v2rayNG</a>
                            <a href="hiddify://install-sub?url=${subUrl}&name=${subName}" class="btn-import"><i class="fa-solid fa-shield-cat text-info"></i> Hiddify</a>
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
                    Powered by <a href="https://github.com/Code-Leafy/G2ray" target="_blank"><i class="fa-brands fa-github"></i> G2ray</a>
                </div>
            `;
        }
        render();
    </script>
</body>
</html>"""

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>G2ray Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <style>
        :root {
            --bg-base: #09090b; --bg-panel: #121214; --bg-hover: #1f1f22; --bg-active: #27272a;
            --border: rgba(255, 255, 255, 0.08); --border-hover: rgba(255, 255, 255, 0.15);
            --text-main: #fafafa; --text-muted: #a1a1aa;
            --accent: #10b981; --accent-hover: #059669; --accent-bg: rgba(16, 185, 129, 0.12);
            --danger: #ef4444; --danger-bg: rgba(239, 68, 68, 0.12);
            --warning: #f59e0b; --warning-bg: rgba(245, 158, 11, 0.12);
            --info: #3b82f6; --info-bg: rgba(59, 130, 246, 0.12);
            --purple: #8b5cf6; --purple-bg: rgba(139, 92, 246, 0.12);
            --radius-lg: 16px; --radius-md: 12px; --radius-sm: 8px; 
            --transition: all 0.2s cubic-bezier(0.2, 0.8, 0.2, 1);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        ::selection { background: rgba(16, 185, 129, 0.3); color: #fff; }
        
        body { background-color: var(--bg-base); color: var(--text-main); font-family: 'Plus Jakarta Sans', sans-serif; font-size: 14px; display: flex; height: 100vh; min-height: 100vh; width: 100vw; overflow: hidden; -webkit-font-smoothing: antialiased; }
        
        input, textarea, select, .mono, pre, code, #log-output, td, .form-label { user-select: text; -webkit-user-select: text; }
        h1, h2, h3, h4, h5 { font-weight: 700; letter-spacing: -0.01em; color: var(--text-main); }
        .mono { font-family: 'JetBrains Mono', monospace; }
        
        .text-accent { color: var(--accent) !important; }
        .text-info { color: var(--info) !important; }
        .text-warning { color: var(--warning) !important; }
        .text-danger { color: var(--danger) !important; }
        .text-purple { color: var(--purple) !important; }
        
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.15); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.25); }
        
        #loader { position: fixed; inset: 0; background: var(--bg-base); z-index: 99999; display: flex; justify-content: center; align-items: center; transition: opacity 0.4s ease, visibility 0.4s; }
        .spinner-ring { width: 40px; height: 40px; border: 3px solid var(--border-hover); border-top: 3px solid var(--accent); border-radius: 50%; animation: spin 0.85s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        
        .sidebar { width: 260px; background-color: var(--bg-panel); border-right: 1px solid var(--border); display: flex; flex-direction: column; z-index: 100; transition: var(--transition); flex-shrink: 0; }
        .logo-box { height: 60px; display: flex; align-items: center; gap: 12px; padding: 0 24px; font-size: 1.15rem; font-weight: 800; color: #fff; flex-shrink: 0; border-bottom: 1px solid var(--border); background: var(--bg-base); }
        .logo-box svg { width: 22px; height: 22px; fill: var(--accent); }
        .nav-menu { flex: 1; overflow-y: auto; padding: 16px 12px; display: flex; flex-direction: column; gap: 6px; }
        .nav-label { font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); font-weight: 800; letter-spacing: 0.08em; margin: 16px 0 8px 12px; }
        .nav-item { padding: 12px 14px; border-radius: var(--radius-sm); cursor: pointer; display: flex; align-items: center; gap: 12px; color: var(--text-muted); font-weight: 600; transition: var(--transition); font-size: 0.85rem; }
        .nav-item i { font-size: 1.05rem; width: 20px; text-align: center; pointer-events: none; transition: var(--transition); }
        .nav-item:hover { background-color: var(--bg-hover); color: var(--text-main); }
        .nav-item.active { background-color: var(--accent-bg); color: var(--accent); }
        .nav-item.active i { color: var(--accent); }
        .sidebar-footer { padding: 14px; text-align: center; font-size: 0.75rem; color: var(--text-muted); font-weight: 600; flex-shrink: 0; border-top: 1px solid var(--border); }
        .sidebar-footer a:hover { color: var(--text-main) !important; }
        
        .app-wrapper { flex: 1; display: flex; flex-direction: column; min-width: 0; background: var(--bg-base); height: 100vh; overflow: hidden; }
        .topbar { height: 60px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 24px; background-color: var(--bg-base); z-index: 50; flex-shrink: 0; }
        .topbar.hidden { display: none; }
        .mini-stats { display: flex; gap: 24px; font-weight: 600; font-size: 0.85rem; font-family: 'JetBrains Mono', monospace; }
        .mini-stat-item { display: flex; align-items: center; gap: 8px; }
        .content-area { flex: 1; padding: 24px; display: flex; flex-direction: column; overflow: hidden; gap: 16px; }
        
        .tab-view { display: none; flex-direction: column; flex: 1; min-height: 0; gap: 16px; animation: slideFadeUp 0.3s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; overflow-y: auto; overflow-x: hidden; padding-right: 4px; padding-bottom: 20px; }
        .tab-view.active { display: flex; }
        @keyframes slideFadeUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

        .header-section { flex-shrink: 0; display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }
        .header-section h2 { font-size: 1.4rem; }
        .header-section p { color: var(--text-muted); font-weight: 500; font-size: 0.85rem; margin-top: 6px; }

        .btn, .btn-icon, .chart-filter-btn { cursor: pointer; }
        .btn { background: var(--bg-hover); color: var(--text-main); border: 1px solid var(--border); padding: 8px 16px; border-radius: var(--radius-sm); font-size: 0.8rem; font-weight: 600; transition: var(--transition); display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-family: inherit; box-shadow: 0 1px 2px rgba(0,0,0,0.2); height: 38px; }
        .btn:hover:not(:disabled) { background: var(--bg-active); border-color: var(--border-hover); transform: translateY(-1px); }
        .btn:active:not(:disabled) { transform: translateY(1px); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-primary { background: var(--accent); color: #000; border: none; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2); }
        .btn-primary:hover:not(:disabled) { background: var(--accent-hover); color: #fff; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.4); }
        .btn-danger { background: var(--danger-bg); color: var(--danger); border: none; }
        .btn-danger:hover:not(:disabled) { background: var(--danger); color: #fff; }
        .btn-icon { padding: 6px; width: 38px; height: 38px; border-radius: var(--radius-sm); border: 1px solid transparent; display: inline-flex; align-items: center; justify-content: center; background: var(--bg-hover); color: var(--text-muted); transition: var(--transition); box-shadow: none; }
        .btn-icon:hover { background: var(--bg-active); color: var(--text-main); transform: translateY(0); }
        .btn-icon.btn-danger { background: transparent; color: var(--danger); }
        .btn-icon.btn-danger:hover { background: var(--danger-bg); }
        
        .panel { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.2); }
        .panel-full { flex: 1; min-height: 0; } 
        .panel-header { padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; background: rgba(255,255,255,0.02); min-height: 64px; }
        .panel-title { font-size: 0.95rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .panel-body { padding: 20px; flex: 1; min-height: 0; display: flex; flex-direction: column; overflow-y: auto; }
        .panel-body-unpadded { padding: 0; flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
        
        .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; flex-shrink: 0; }
        .grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; flex-shrink: 0; }
        .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; flex-shrink: 0; }
        .grid-1-2 { display: grid; grid-template-columns: 1fr 2fr; gap: 16px; flex-shrink: 0; }
        
        .metric-card { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 20px; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.2); position: relative; overflow: hidden; }
        .metric-title { font-size: 0.75rem; color: var(--text-muted); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; display: flex; justify-content: space-between; align-items: center; z-index: 2; }
        .metric-val { font-size: 2rem; font-weight: 800; color: var(--text-main); display: flex; align-items: baseline; gap: 8px; letter-spacing: -0.02em; z-index: 2; }
        .metric-sub { font-size: 0.85rem; color: var(--text-muted); font-weight: 600; }
        
        .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .form-group { display: flex; flex-direction: column; gap: 8px; margin-bottom: 4px; }
        .form-label { color: var(--text-muted); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 2px; }
        .form-control { width: 100%; padding: 8px 12px; background: var(--bg-hover); border: 1px solid var(--border); color: var(--text-main); border-radius: var(--radius-sm); font-size: 0.85rem; transition: var(--transition); font-family: inherit; font-weight: 500; height: 38px; }
        textarea.form-control { height: auto; }
        .form-control:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-bg); background: var(--bg-active); }
        input.form-control:read-only, input.form-control:disabled, textarea.form-control:read-only, textarea.form-control:disabled { background: var(--bg-base); color: var(--text-muted); cursor: not-allowed; opacity: 1; border-color: transparent; }
        select.form-control { -webkit-appearance: none; appearance: none; cursor: pointer; background-image: url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23a1a1aa' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3e%3cpolyline points='6 9 12 15 18 9'%3e%3c/polyline%3e%3c/svg%3e"); background-repeat: no-repeat; background-position: right 14px center; background-size: 14px; padding-right: 36px; }
        select.form-control:not(:disabled) { color: var(--text-main) !important; background-color: var(--bg-hover) !important; }
        select.form-control option { background-color: #1f1f22; color: #fafafa; }
        
        .input-group { display: flex; gap: 8px; }
        .switch { position: relative; display: inline-block; width: 38px; height: 20px; flex-shrink: 0; cursor: pointer; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; inset: 0; background-color: var(--bg-hover); border: 1px solid var(--border); transition: 0.3s ease; border-radius: 20px; }
        .slider:before { position: absolute; content: ""; height: 14px; width: 14px; left: 2px; bottom: 2px; background-color: var(--text-muted); border-radius: 50%; transition: 0.3s ease; }
        input:checked + .slider { background-color: var(--accent); border-color: var(--accent); }
        input:checked + .slider:before { transform: translateX(18px); background-color: #fff; }
        
        .table-wrap { flex: 1; overflow-y: auto; overflow-x: auto; background: var(--bg-panel); min-height: 0; }
        table { width: 100%; border-collapse: collapse; text-align: left; white-space: nowrap; font-size: 0.85rem; }
        th { position: sticky; top: 0; background: var(--bg-hover); color: var(--text-muted); font-weight: 700; font-size: 0.7rem; padding: 12px 20px; text-transform: uppercase; letter-spacing: 0.05em; z-index: 10; border-bottom: 1px solid var(--border); box-shadow: 0 1px 0 var(--border); }
        td { padding: 14px 20px; border-bottom: 1px solid var(--border); font-weight: 500; vertical-align: middle; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255, 255, 255, 0.03); }
        
        .tag { padding: 4px 10px; border-radius: 6px; font-size: 0.65rem; font-weight: 700; text-transform: uppercase; display: inline-flex; align-items: center; letter-spacing: 0.05em; }
        .tag-green { background: var(--accent-bg); color: var(--accent); }
        .tag-red { background: var(--danger-bg); color: var(--danger); }
        .tag-blue { background: var(--info-bg); color: var(--info); }
        .tag-purple { background: var(--purple-bg); color: var(--purple); }
        .tag-warn { background: var(--warning-bg); color: var(--warning); }
        
        .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(5px); display: none; justify-content: center; align-items: center; z-index: 1000; opacity: 0; transition: opacity 0.25s; padding: 20px; }
        .modal-overlay.show { display: flex; opacity: 1; }
        .modal { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); width: 100%; max-width: 600px; transform: scale(0.95) translateY(15px); transition: transform 0.25s cubic-bezier(0.2, 0.8, 0.2, 1); display: flex; flex-direction: column; max-height: 100%; box-shadow: 0 24px 48px rgba(0,0,0,0.6); }
        .modal-overlay.show .modal { transform: scale(1) translateY(0); }
        .modal-header { padding: 18px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; background: rgba(255,255,255,0.02); border-radius: var(--radius-md) var(--radius-md) 0 0; }
        .modal-tabs { display: flex; border-bottom: 1px solid var(--border); background: var(--bg-panel); padding: 0 12px; flex-shrink: 0; }
        .modal-tab-btn { background: transparent; border: none; color: var(--text-muted); padding: 14px 20px; font-weight: 700; font-size: 0.8rem; cursor: pointer; border-bottom: 2px solid transparent; transition: var(--transition); text-transform: uppercase; letter-spacing: 0.05em; }
        .modal-tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
        .modal-body { padding: 24px; overflow-y: auto; flex: 1; gap: 16px; display: flex; flex-direction: column; }
        .modal-tab-content { display: none; flex-direction: column; gap: 16px; }
        .modal-tab-content.active { display: flex; animation: slideFadeUp 0.2s ease forwards; }
        .modal-footer { padding: 18px 24px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 12px; flex-shrink: 0; background: rgba(255,255,255,0.01); border-radius: 0 0 var(--radius-md) var(--radius-md); }
        
        .chart-wrapper { position: relative; width: 100%; height: 100%; min-height: 0; min-width: 0; flex: 1; display: flex; align-items: center; justify-content: center; }
        .map-container { width: 100%; height: 160px; background: var(--bg-base); position: relative; overflow: hidden; display: flex; justify-content: center; align-items: center; border-bottom: 1px solid var(--border); background-image: radial-gradient(var(--border) 1px, transparent 1px); background-size: 20px 20px; flex-shrink: 0; }
        @keyframes sweep { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        @keyframes pulseDot { 0% { transform: scale(1); box-shadow: 0 0 10px var(--accent); } 50% { transform: scale(1.5); box-shadow: 0 0 25px var(--accent); } 100% { transform: scale(1); box-shadow: 0 0 10px var(--accent); } }
        .radar-sweep { position: absolute; width: 300px; height: 300px; border-radius: 50%; background: conic-gradient(from 0deg, rgba(16,185,129,0) 70%, rgba(16,185,129,0.15) 100%); animation: sweep 3s linear infinite; }
        .map-dot { position: absolute; width: 6px; height: 6px; background: var(--accent); border-radius: 50%; box-shadow: 0 0 10px var(--accent); animation: pulseDot 2s infinite; z-index: 2; }
        
        .terminal { background: #050505; color: #a1a1aa; padding: 20px; font-size: 0.8rem; line-height: 1.6; flex: 1; overflow-y: auto; border-radius: 0 0 var(--radius-md) var(--radius-md); user-select: text; white-space: pre-wrap; font-family: 'JetBrains Mono', monospace; }
        
        .toast-box { position: fixed; bottom: 24px; right: 24px; display: flex; flex-direction: column; gap: 12px; z-index: 9999; pointer-events: none; }
        .toast { background: var(--bg-panel); border: 1px solid var(--border); padding: 14px 20px; border-radius: var(--radius-md); display: flex; align-items: center; gap: 12px; box-shadow: 0 12px 24px rgba(0,0,0,0.4); font-weight: 600; font-size: 0.85rem; pointer-events: auto; border-left: 4px solid var(--accent); animation: slideFadeUp 0.25s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; color: var(--text-main); }
        
        .hw-bar-bg { height: 6px; background: var(--bg-hover); border-radius: 4px; overflow: hidden; margin-top: 6px; }
        .hw-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s cubic-bezier(0.2, 0.8, 0.2, 1); }
        .qr-wrapper { background: #fff; padding: 20px; border-radius: var(--radius-sm); display: inline-block; border: 4px solid var(--bg-hover); margin: 0 auto; }
        
        .settings-row { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; border-bottom: 1px solid var(--border); gap: 16px; }
        .settings-row:last-child { border-bottom: none; padding-bottom: 0; }
        .settings-row:first-child { padding-top: 0; }
        .settings-info h4 { font-size: 0.85rem; margin-bottom: 4px; color: var(--text-main); }
        .settings-info p { font-size: 0.75rem; color: var(--text-muted); line-height: 1.4; }
        
        .checkbox-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; width: 100%; }
        .custom-checkbox { display: flex; align-items: center; gap: 10px; font-size: 0.8rem; font-weight: 600; color: var(--text-muted); cursor: pointer; transition: var(--transition); }
        .custom-checkbox:hover { color: var(--text-main); }
        .custom-checkbox input { accent-color: var(--accent); width: 16px; height: 16px; cursor: pointer; }

        .mobile-toggle { display: none; background: none; border: none; color: var(--text-main); font-size: 1.2rem; cursor: pointer; padding: 4px; }

        .sublab-layout { display: grid; grid-template-columns: 1fr 340px; gap: 20px; flex: 1; min-height: 500px; flex-shrink: 0; }
        .sublab-editor { display: flex; flex-direction: column; gap: 16px; min-height: 0; overflow-y: auto; padding-right: 4px; }
        .sublab-preview { display: flex; flex-direction: column; gap: 16px; min-height: 0; }

        .phone-mockup-wrapper { display: flex; justify-content: center; overflow: hidden; flex: 1; min-height: 0; padding: 10px; }
        .phone-mockup { background: #0a0a0c; border: 2px solid rgba(255,255,255,0.08); border-radius: 40px; display: flex; flex-direction: column; align-items: center; box-shadow: 0 24px 60px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.04); width: 100%; max-width: 300px; height: 100%; position: relative; overflow: hidden; }
        .phone-notch { width: 130px; height: 26px; background: #0a0a0c; border-radius: 0 0 16px 16px; position: absolute; top: 0; left: 50%; transform: translateX(-50%); z-index: 10; }
        .phone-screen { flex: 1; width: 100%; background: #111113; border-radius: 38px; display: flex; flex-direction: column; padding-top: 36px; min-height: 0; overflow: hidden; border: 8px solid #0a0a0c; }
        .phone-config-list { flex: 1; overflow-y: auto; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
        
        .phone-config-list::-webkit-scrollbar { width: 2px; }
        .phone-config-list::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.1); border-radius: 4px; }
        .phone-config-list:hover::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.3); }

        .phone-item { position: relative; background: #1c1c1f; border-radius: 12px; padding: 12px 14px; display: flex; align-items: center; gap: 12px; cursor: default; border: 1px solid rgba(255,255,255,0.04); transition: var(--transition); }
        .phone-item:hover { background: #222225; border-color: rgba(255,255,255,0.1); }
        .phone-item.info-item { background: linear-gradient(135deg, rgba(16,185,129,0.05), rgba(59,130,246,0.04)); border-color: rgba(16,185,129,0.1); }
        .phone-item-icon { width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 0.85rem; background: rgba(0,0,0,0.2); }
        .phone-item-body { flex: 1; min-width: 0; }
        .phone-item-name { font-size: 0.75rem; font-weight: 700; color: #e4e4e7; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .phone-item-sub { font-size: 0.65rem; color: #71717a; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .phone-item-action { margin-left: auto; opacity: 0; transition: var(--transition); }
        .phone-item:hover .phone-item-action { opacity: 1; }

        .sub-entry { background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px 16px; display: flex; align-items: flex-start; gap: 12px; cursor: grab; transition: var(--transition); }
        .sub-entry:hover { border-color: var(--border-hover); box-shadow: 0 4px 12px rgba(0,0,0,0.2); }
        .sub-entry-drag { color: var(--text-muted); font-size: 1rem; flex-shrink: 0; cursor: grab; padding-top: 6px; }
        .sub-entry-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
        .sub-entry-type { font-size: 0.65rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }

        .ph-chip { font-size: 0.68rem; font-family: 'JetBrains Mono', monospace; color: var(--info); background: var(--info-bg); padding: 6px 8px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; transition: var(--transition); user-select: none; text-align: center; font-weight: 600; }
        .ph-chip:hover { border-color: var(--info); background: rgba(59,130,246,0.2); transform: translateY(-1px); }

        .collapsible-header { cursor: pointer; }
        .collapsible-header:hover { background: rgba(255,255,255,0.03); }
        .collapsible-body { transition: max-height 0.25s ease, opacity 0.2s ease, padding 0.2s ease; overflow: hidden; }
        .collapsible-body.collapsed { max-height: 0 !important; padding-top: 0 !important; padding-bottom: 0 !important; opacity: 0; }
        .collapse-icon.collapsed { transform: rotate(180deg); }

        @media (max-width: 1280px) { .content-area { padding: 16px; } }
        @media (max-width: 1024px) {
            .grid-4, .grid-3 { grid-template-columns: repeat(2, 1fr); }
            .grid-1-2 { grid-template-columns: 1fr; }
            .sidebar { position: fixed; left: -260px; top: 0; bottom: 0; box-shadow: 10px 0 30px rgba(0,0,0,0.6); }
            .sidebar.open { left: 0; }
            .mobile-toggle { display: block; }
            .topbar { display: flex !important; }
            .mini-stats { display: none !important; }
            .content-area { padding: 16px; }
        }
        @media (max-width: 600px) {
            .grid-4, .grid-3, .grid-2, .form-grid { grid-template-columns: 1fr; }
            .header-section { flex-direction: column; align-items: flex-start; }
            .checkbox-grid { grid-template-columns: 1fr; }
            .modal-tabs { overflow-x: auto; white-space: nowrap; }
            .content-area { padding: 12px; }
            .panel-header { padding: 14px 16px; }
            .panel-body { padding: 16px; }
            .table-wrap { overflow-x: auto; }
            .sublab-layout { grid-template-columns: 1fr; }
            .metric-val { font-size: 1.5rem; }
            .phone-mockup-wrapper { min-height: 500px; flex: none; }
        }
    </style>
</head>
<body>
    <div id="loader"><div class="spinner-ring"></div></div>
    
    <div id="auth-overlay" class="modal-overlay" style="display:none; opacity:1; z-index:100000; background: var(--bg-base); flex-direction:column; justify-content:center; align-items:center;">
        <div class="logo-box" style="margin-bottom:24px; border:none; background:transparent; padding:0;">
            <svg viewBox="0 0 496 512" fill="var(--accent)"><path d="M165.9 397.4c0 2-2.3 3.6-5.2 3.6-3.3.3-5.6-1.3-5.6-3.6 0-2 2.3-3.6 5.2-3.6 3-.3 5.6 1.3 5.6 3.6zm-31.1-4.5c-.7 2 1.3 4.3 4.3 4.9 2.6 1 5.6 0 6.2-2s-1.3-4.3-4.3-5.2c-2.6-.7-5.5.3-6.2 2.3zm44.2-1.7c-2.9.7-4.9 2.6-4.6 4.9.3 2 2.9 3.3 5.9 2.6 2.9-.7 4.9-2.6 4.6-4.6-.3-1.9-3-3.2-5.9-2.9zM244.8 8C106.1 8 0 113.3 0 252c0 110.9 69.8 205.8 169.5 239.2 12.8 2.3 17.3-5.6 17.3-12.1 0-6.2-.3-40.4-.3-61.4 0 0-70 15-84.7-29.8 0 0-11.4-29.1-27.8-36.6 0 0-22.9-15.7 1.6-15.4 0 0 24.9 2 38.6 25.8 21.9 38.6 58.6 27.5 72.9 20.9 2.3-16 8.8-27.1 16-33.7-55.9-6.2-112.3-14.3-112.3-110.5 0-27.5 7.6-41.3 23.6-58.9-2.6-6.5-11.1-33.3 2.6-67.9 20.9-6.5 69 27 69 27 20-5.6 41.5-8.5 62.8-8.5s42.8 2.9 62.8 8.5c0 0 48.1-33.6 69-27 13.7 34.7 5.2 61.4 2.6 67.9 16 17.7 25.8 31.5 25.8 58.9 0 96.5-58.9 104.2-114.8 110.5 9.2 7.9 17 22.9 17 46.4 0 33.7-.3 75.4-.3 83.6 0 6.5 4.6 14.4 17.3 12.1C428.2 457.8 496 362.9 496 252 496 113.3 383.5 8 244.8 8zM97.2 352.9c-1.3 1-1 3.3.7 5.2 1.6 1.6 3.9 2.3 5.2 1 1.3-1 1-3.3-.7-5.2-1.6-1.6-3.9-2.3-5.2-1zm-10.8-8.1c-.7 1.3.3 2.9 2.3 3.9 1.6 1 3.6.7 4.3-.7.7-1.3-.3-2.9-2.3-3.9-2-.6-3.6-.3-4.3.7zm32.4 35.6c-1.6 1.3-1 4.3 1.3 6.2 2.3 2.3 5.2 2.6 6.5 1 1.3-1.3.7-4.3-1.3-6.2-2.2-2.3-5.2-2.6-6.5-1zm-11.4-14.7c-1.6 1-1.6 3.6 0 5.9 1.6 2.3 4.3 3.3 5.6 2.3 1.6-1.3 1.6-3.9 0-6.2-1.4-2.3-4-3.3-5.6-2z"/></svg>
            <span style="font-size:1.8rem; font-weight:800; color:#fff;">G2ray<span style="color:var(--text-muted); font-weight:500;">Panel</span></span>
        </div>
        <div class="modal show" style="max-width: 420px; width: 100%; margin:0 20px; position:relative; transform:none; box-shadow:0 24px 60px rgba(0,0,0,0.8);">
            <div class="modal-header" style="justify-content:center; padding:20px;"><div class="panel-title" style="font-size:1.1rem;"><i class="fa-solid fa-lock text-accent"></i> Authentication Required</div></div>
            <div class="modal-body" id="auth-body" style="padding:24px;"></div>
        </div>
    </div>
    
    <aside class="sidebar" id="sidebar">
        <div class="logo-box">
            <svg viewBox="0 0 496 512" fill="var(--accent)"><path d="M165.9 397.4c0 2-2.3 3.6-5.2 3.6-3.3.3-5.6-1.3-5.6-3.6 0-2 2.3-3.6 5.2-3.6 3-.3 5.6 1.3 5.6 3.6zm-31.1-4.5c-.7 2 1.3 4.3 4.3 4.9 2.6 1 5.6 0 6.2-2s-1.3-4.3-4.3-5.2c-2.6-.7-5.5.3-6.2 2.3zm44.2-1.7c-2.9.7-4.9 2.6-4.6 4.9.3 2 2.9 3.3 5.9 2.6 2.9-.7 4.9-2.6 4.6-4.6-.3-1.9-3-3.2-5.9-2.9zM244.8 8C106.1 8 0 113.3 0 252c0 110.9 69.8 205.8 169.5 239.2 12.8 2.3 17.3-5.6 17.3-12.1 0-6.2-.3-40.4-.3-61.4 0 0-70 15-84.7-29.8 0 0-11.4-29.1-27.8-36.6 0 0-22.9-15.7 1.6-15.4 0 0 24.9 2 38.6 25.8 21.9 38.6 58.6 27.5 72.9 20.9 2.3-16 8.8-27.1 16-33.7-55.9-6.2-112.3-14.3-112.3-110.5 0-27.5 7.6-41.3 23.6-58.9-2.6-6.5-11.1-33.3 2.6-67.9 20.9-6.5 69 27 69 27 20-5.6 41.5-8.5 62.8-8.5s42.8 2.9 62.8 8.5c0 0 48.1-33.6 69-27 13.7 34.7 5.2 61.4 2.6 67.9 16 17.7 25.8 31.5 25.8 58.9 0 96.5-58.9 104.2-114.8 110.5 9.2 7.9 17 22.9 17 46.4 0 33.7-.3 75.4-.3 83.6 0 6.5 4.6 14.4 17.3 12.1C428.2 457.8 496 362.9 496 252 496 113.3 383.5 8 244.8 8zM97.2 352.9c-1.3 1-1 3.3.7 5.2 1.6 1.6 3.9 2.3 5.2 1 1.3-1 1-3.3-.7-5.2-1.6-1.6-3.9-2.3-5.2-1zm-10.8-8.1c-.7 1.3.3 2.9 2.3 3.9 1.6 1 3.6.7 4.3-.7.7-1.3-.3-2.9-2.3-3.9-2-.6-3.6-.3-4.3.7zm32.4 35.6c-1.6 1.3-1 4.3 1.3 6.2 2.3 2.3 5.2 2.6 6.5 1 1.3-1.3.7-4.3-1.3-6.2-2.2-2.3-5.2-2.6-6.5-1zm-11.4-14.7c-1.6 1-1.6 3.6 0 5.9 1.6 2.3 4.3 3.3 5.6 2.3 1.6-1.3 1.6-3.9 0-6.2-1.4-2.3-4-3.3-5.6-2z"/></svg>
            G2ray<span style="color:var(--text-muted); font-weight:500;">Panel</span>
        </div>
        <div class="nav-menu">
            <div class="nav-label">Core Analytics</div>
            <div class="nav-item active" onclick="switchTab('dashboard')"><i class="fa-solid fa-chart-pie"></i> Dashboard</div>
            <div class="nav-label">Environment</div>
            <div class="nav-item" onclick="switchTab('codespace')"><i class="fa-brands fa-github"></i> Codespace Settings</div>
            <div class="nav-label">Traffic Routing</div>
            <div class="nav-item" onclick="switchTab('clients')"><i class="fa-solid fa-users"></i> Client Profiles</div>
            <div class="nav-item" onclick="switchTab('sublab')"><i class="fa-solid fa-flask"></i> Subscription Lab</div>
            <div class="nav-label">System</div>
            <div class="nav-item" onclick="switchTab('settings')"><i class="fa-solid fa-gear"></i> Advanced Settings</div>
            <div class="nav-item" onclick="switchTab('logs')"><i class="fa-solid fa-terminal"></i> Console Logs</div>
        </div>
        <div class="sidebar-footer">
            <div style="margin-bottom: 8px;">
                Built with <i class="fa-solid fa-mug-hot text-accent"></i> by <a href="https://github.com/Code-Leafy" target="_blank" style="color:var(--text-main); text-decoration:none; font-weight:700;">Code-Leafy</a>
            </div>
            <div>
                <a href="https://github.com/Code-Leafy/G2ray" target="_blank" style="color:var(--text-muted); text-decoration:none; transition: var(--transition); font-weight:700;"><i class="fa-brands fa-github"></i> G2ray</a>
            </div>
        </div>
    </aside>

    <main class="app-wrapper">
        <header class="topbar hidden" id="main-topbar">
            <button class="mobile-toggle" onclick="document.getElementById('sidebar').classList.toggle('open')"><i class="fa-solid fa-bars"></i></button>
            <div class="mini-stats" id="mini-stats">
                <div class="mini-stat-item" style="color:var(--accent)"><i class="fa-solid fa-arrow-down-long"></i> <span id="m-rx-mini">0.00</span> GB</div>
                <div class="mini-stat-item" style="color:var(--info)"><i class="fa-solid fa-arrow-up-long"></i> <span id="m-tx-mini">0.00</span> GB</div>
                <div class="mini-stat-item" style="color:var(--purple)"><i class="fa-solid fa-gauge-high"></i> <span id="m-speed-mini">0 / 0</span> Mbps</div>
            </div>
            <div id="topbar-xray-status" style="display:flex; align-items:center; gap:8px; font-size:0.78rem; font-weight:700; font-family:'JetBrains Mono',monospace;">
                <span id="topbar-xray-dot" style="width:8px; height:8px; border-radius:50%; background:var(--accent); box-shadow:0 0 6px var(--accent); display:inline-block; flex-shrink:0; transition:background 0.3s, box-shadow 0.3s;"></span>
                <span id="topbar-xray-label" style="color:var(--accent); transition:color 0.3s;">Xray ON</span>
            </div>
        </header>
        
        <button class="mobile-toggle" style="position:absolute; top:16px; left:20px; z-index:40;" id="mobile-dash-btn" onclick="document.getElementById('sidebar').classList.toggle('open')"><i class="fa-solid fa-bars"></i></button>

        <div class="content-area">
            
            <div id="tab-dashboard" class="tab-view active">
                <div class="header-section"><div><h2>System Dashboard</h2><p>Real-time telemetry and core engine controls.</p></div></div>
                <div class="grid-4">
                    <div class="metric-card"><div class="metric-title">Download <i class="fa-solid fa-arrow-down-long" style="color:var(--accent)"></i></div><div class="metric-val mono" id="m-rx">0.00 <span class="metric-sub">GB</span></div></div>
                    <div class="metric-card"><div class="metric-title">Upload <i class="fa-solid fa-arrow-up-long" style="color:var(--info)"></i></div><div class="metric-val mono" id="m-tx">0.00 <span class="metric-sub">GB</span></div></div>
                    <div class="metric-card"><div class="metric-title">Speed (DL/UL) <i class="fa-solid fa-gauge-high" style="color:var(--purple)"></i></div><div class="metric-val mono" id="m-speed">0 <span class="metric-sub">/ 0 Mbps</span></div></div>
                    <div class="metric-card"><div class="metric-title">Core Uptime <i class="fa-solid fa-clock" style="color:var(--warning)"></i></div><div class="metric-val mono" id="m-uptime">0h 00m</div></div>
                </div>

                <div class="grid-2" style="flex: 1; min-height: 320px; flex-shrink: 0;">
                    <div style="display:flex; flex-direction:column; gap:16px; flex: 1;">
                        <div class="panel" style="flex: 1;">
                            <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-heart-pulse text-accent"></i> Health Overview</div></div>
                            <div class="panel-body" style="gap: 16px; justify-content: center;">
                                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:12px;"><span style="color:var(--text-muted); font-weight:600; font-size:0.85rem;">Engine Status</span><span class="tag tag-red" id="dash-status-tag">Offline</span></div>
                                <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:12px;"><span style="color:var(--text-muted); font-weight:600; font-size:0.85rem;">System Load Avg</span><span class="mono" id="dash-load-avg">0.00</span></div>
                                <div style="display:flex; justify-content:space-between; align-items:center;"><span style="color:var(--text-muted); font-weight:600; font-size:0.85rem;">Memory Allocation</span><span class="mono" id="dash-mem-alloc">0 / 0 MB</span></div>
                            </div>
                        </div>
                        <div class="panel">
                            <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-power-off text-info"></i> Power Control</div></div>
                            <div class="panel-body" style="gap: 16px; justify-content: center;">
                                <p style="color:var(--text-muted); font-size:0.8rem; line-height:1.5;">Restarting or stopping the core will immediately sever all active client connections.</p>
                                <div style="display:flex; gap:12px;">
                                    <button class="btn" style="flex:1; background:var(--accent-bg); color:var(--accent); border:none;" onclick="window.setXrayStatus('start')"><i class="fa-solid fa-play"></i> Start</button>
                                    <button class="btn" style="flex:1; background:var(--danger-bg); color:var(--danger); border:none;" onclick="window.setXrayStatus('stop')"><i class="fa-solid fa-stop"></i> Stop</button>
                                    <button class="btn btn-primary" style="flex:1;" onclick="window.setXrayStatus('restart')"><i class="fa-solid fa-rotate-right"></i> Restart</button>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="panel" style="flex: 1;">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-wave-square text-accent"></i> Global Traffic Flow</div></div>
                        <div class="panel-body-unpadded chart-wrapper" style="padding: 16px;"><canvas id="chart-traffic"></canvas></div>
                    </div>
                </div>
            </div>

            <div id="tab-codespace" class="tab-view">
                <div class="header-section">
                    <div><h2>Codespace Environment</h2><p>Manage GitHub VM instances, location, and hardware limitations.</p></div>
                </div>
                
                <div class="grid-2" style="flex-shrink: 0;">
                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-location-crosshairs text-info"></i> IP & Region Mapping</div></div>
                        <div class="panel-body-unpadded">
                            <div class="map-container" style="height: 120px;">
                                <div class="radar-sweep"></div><div class="map-dot"></div>
                            </div>
                            <div style="padding:20px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
                                <div><div class="form-label">City</div><div id="cs-city" style="font-weight:600; font-size:0.85rem;">N/A</div></div>
                                <div><div class="form-label">Country</div><div id="cs-country" style="font-weight:600; font-size:0.85rem;">N/A</div></div>
                                <div><div class="form-label">Public IPv4</div><div id="cs-ipv4" class="mono" style="color:var(--info); font-weight:600; font-size:0.85rem;">N/A</div></div>
                                <div><div class="form-label">Provider</div><div id="cs-provider" style="font-weight:600; font-size:0.85rem;">N/A</div></div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-wallet text-accent"></i> Compute Quota</div></div>
                        <div class="panel-body" style="gap:16px;">
                            <div class="form-group" style="flex-shrink:0;">
                                <label class="form-label" style="display:flex; justify-content:space-between;"><span>Fund Balance ($)</span><span style="color:var(--accent);">1$ = 20h</span></label>
                                <input type="number" class="form-control" id="quota-dollars" placeholder="0.00" value="0" min="0" step="1" oninput="window.schedulePanelSync('quota')">
                            </div>
                            <div style="display:flex; flex-direction:column; gap:10px; flex-shrink:0;">
                                <div style="display:flex; justify-content:space-between; font-weight:700; font-size:0.8rem;"><span id="q-used" style="color:var(--danger);">Used: 0h</span><span id="q-rem" style="color:var(--accent);">Remaining: 60h</span></div>
                                <div class="hw-bar-bg" style="margin-top:0;"><div id="q-bar" class="hw-bar-fill" style="width:0%; background:var(--text-muted);"></div></div>
                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:6px;">
                                    <div style="background:var(--bg-hover); border:1px solid var(--border); border-radius:10px; padding:12px;"><div class="form-label" style="margin-bottom:4px;">Base allowance</div><div id="q-base-hours" class="mono" style="font-weight:700; font-size:0.95rem;">60h</div></div>
                                    <div style="background:var(--bg-hover); border:1px solid var(--border); border-radius:10px; padding:12px;"><div class="form-label" style="margin-bottom:4px;">Funded hours</div><div id="q-funded-hours" class="mono" style="font-weight:700; font-size:0.95rem; color:var(--accent);">0h</div></div>
                                </div>
                            </div>
                            <div style="display:flex; justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:16px; margin-top:auto;">
                                <div><div style="font-weight:700; font-size:0.85rem;">Wake Lock</div><div style="font-size:0.75rem; color:var(--text-muted); margin-top:4px;">Prevents VM suspension.</div></div>
                                <label class="switch"><input type="checkbox" id="wake-lock-toggle" onchange="window.toggleWakeLock(this)"><span class="slider"></span></label>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="panel" style="flex: 1; min-height: 320px; flex-shrink: 0;">
                    <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-microchip text-warning"></i> Hardware Utilization</div></div>
                    <div class="panel-body grid-2" style="align-items: center; gap: 24px; overflow: hidden; padding: 20px;">
                        <div class="chart-wrapper" style="height: 100%; min-height: 0;"><canvas id="chart-hardware"></canvas></div>
                        <div style="display: flex; flex-direction: column; gap: 20px; padding-right: 16px;">
                            <div><div style="font-size:0.85rem; color:var(--text-muted); display:flex; justify-content:space-between; font-weight:700; margin-bottom:8px;"><span>CPU Load (2 vCores)</span> <span id="hw-cpu-val" class="mono text-main">0%</span></div><div class="hw-bar-bg"><div id="hw-cpu-bar" class="hw-bar-fill" style="width: 0%; background:var(--warning);"></div></div></div>
                            <div><div style="font-size:0.85rem; color:var(--text-muted); display:flex; justify-content:space-between; font-weight:700; margin-bottom:8px;"><span>Memory Usage</span> <span id="hw-ram-val" class="mono text-main">0 MB</span></div><div class="hw-bar-bg"><div id="hw-ram-bar" class="hw-bar-fill" style="width: 0%; background:var(--purple);"></div></div></div>
                            <div><div style="font-size:0.85rem; color:var(--text-muted); display:flex; justify-content:space-between; font-weight:700; margin-bottom:8px;"><span>Disk Storage (GB)</span> <span id="hw-disk-val" class="mono text-main">0 GB</span></div><div class="hw-bar-bg"><div id="hw-disk-bar" class="hw-bar-fill" style="width: 0%; background:var(--info);"></div></div></div>
                        </div>
                    </div>
                </div>
            </div>

            <div id="tab-clients" class="tab-view">
                <div class="header-section">
                    <div><h2>Client Profiles</h2><p>Manage, provision, and monitor individual access credentials.</p></div>
                    <div style="display:flex; gap:12px;">
                        <button class="btn" style="background:var(--purple-bg); color:var(--purple); border:none;" onclick="window.openDonateModal()"><i class="fa-solid fa-gift"></i> Donate Config</button>
                        <button class="btn btn-primary" onclick="window.openAddClientModal()"><i class="fa-solid fa-user-plus"></i> Create Client</button>
                    </div>
                </div>
                <div class="grid-1-2" style="height: 240px; flex-shrink:0;">
                    <div class="panel"><div class="panel-header"><div class="panel-title"><i class="fa-solid fa-chart-pie text-accent"></i> Usage Share</div></div><div class="panel-body chart-wrapper" style="padding: 16px;"><canvas id="client-pie-chart"></canvas></div></div>
                    <div class="panel">
                        <div class="panel-header" style="position:relative;"><div class="panel-title"><i class="fa-solid fa-users-rays text-info"></i> Live Data Flow</div></div>
                        <div class="panel-body-unpadded chart-wrapper" style="padding: 16px 16px 16px 8px;"><canvas id="client-flow-chart"></canvas></div>
                    </div>
                </div>
                <div class="panel panel-full">
                    <div class="table-wrap"><table id="tbl-clients"><thead><tr><th>Remarks / SubID</th><th>uTLS</th><th>Data Usage</th><th>Expiry Date</th><th>Status</th><th style="text-align:right;">Actions</th></tr></thead><tbody></tbody></table></div>
                </div>
            </div>

            <div id="tab-sublab" class="tab-view">
                <div class="header-section" style="flex-shrink:0;">
                    <div style="flex:1;"><h2>Subscription Lab</h2><p>Build, customize, and preview subscription configs for any client.</p></div>
                </div>

                <div class="sublab-layout">
                    <div class="sublab-editor">
                        <div class="panel" style="flex-shrink:0;">
                            <div class="panel-header collapsible-header" onclick="togglePanel(this)"><div class="panel-title"><i class="fa-solid fa-user-gear text-accent"></i> Target Client</div><i class="fa-solid fa-chevron-up collapse-icon" style="color:var(--text-muted); font-size:0.8rem; transition:transform 0.2s;"></i></div>
                            <div class="collapsible-body panel-body" style="padding: 16px; gap: 16px; overflow:hidden;">
                                <select class="form-control" id="sub-client" onchange="window.onSubClientChange()" style="width: 100%;"><option value="">— Select Client —</option></select>
                                <button class="btn btn-primary" onclick="window.saveSubscriptionForClient()" style="width: 100%;"><i class="fa-solid fa-floppy-disk"></i> Save Configuration</button>
                            </div>
                        </div>

                        <div class="panel" style="flex:1; min-height:0;">
                            <div class="panel-header">
                                <div class="panel-title"><i class="fa-solid fa-list text-info"></i> Config Entries <span id="sub-entry-count" class="tag tag-blue" style="margin-left:8px;">0</span></div>
                                <div style="display:flex; gap:8px;">
                                    <select id="transport-sel" class="form-control" style="width: 130px; height: 30px; padding: 4px; font-size: 0.75rem;">
                                        <option value="xhttp">xHTTP (Rec.)</option>
                                        <option value="ws">WebSocket</option>
                                    </select>
                                    <button class="btn" style="padding:4px 10px; height: 30px; font-size:0.75rem;" onclick="window.addSubEntry('proxy')"><i class="fa-solid fa-plus"></i> Proxy</button>
                                    <button class="btn" style="padding:4px 10px; height: 30px; font-size:0.75rem; background:var(--info-bg); color:var(--info); border:none;" onclick="window.addSubEntry('info')"><i class="fa-solid fa-circle-info"></i> Info</button>
                                </div>
                            </div>
                            <div class="panel-body-unpadded" style="overflow-y:auto; padding:16px;">
                                <div id="sub-entries-list" style="display:flex; flex-direction:column; gap:12px; min-height:60px;">
                                    <div id="sub-empty-hint" style="color:var(--text-muted); font-size:0.85rem; text-align:center; padding:30px 0;">Select a client and click <strong>+ Proxy</strong> or <strong>Info</strong>.</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="sublab-preview">
                        <div class="panel" style="flex-shrink:0;">
                            <div class="panel-header collapsible-header" onclick="togglePanel(this)"><div class="panel-title"><i class="fa-solid fa-code text-accent"></i> Placeholders</div><i class="fa-solid fa-chevron-up collapse-icon" style="color:var(--text-muted); font-size:0.8rem; transition:transform 0.25s;"></i></div>
                            <div class="collapsible-body panel-body" style="padding:16px; gap:8px; overflow:hidden;">
                                <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%client-name%')">%client-name%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%quota-used%')">%quota-used%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%quota-remain%')">%quota-remain%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%quota-total%')">%quota-total%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%data-used%')">%data-used%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%data-remain%')">%data-remain%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%data-total%')">%data-total%</div>
                                    <div class="ph-chip" onclick="window.copyPlaceholder('%expiry-date%')">%expiry-date%</div>
                                </div>
                                <p style="font-size:0.7rem; color:var(--text-muted); margin-top:8px; text-align:center;">Click to copy placeholder to clipboard.</p>
                            </div>
                        </div>

                        <div class="panel" style="flex:1; min-height:0;">
                            <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-mobile-screen text-purple"></i> Live Preview</div></div>
                            <div class="panel-body-unpadded phone-mockup-wrapper">
                                <div class="phone-mockup">
                                    <div class="phone-notch"></div>
                                    <div class="phone-screen">
                                        <div class="phone-config-list" id="phone-config-list" ondragover="event.preventDefault()">
                                            <div style="color:#52525b; font-size:0.75rem; text-align:center; padding:30px 0;">No configs yet</div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <div id="tab-settings" class="tab-view">
                <div class="header-section">
                    <div><h2>Advanced Core Settings</h2><p>Configure DNS mapping, Multiplexing, and Core parameters.</p></div>
                    <div style="display:flex; gap:12px; flex-wrap:wrap;">
                        <button class="btn" onclick="window.exportPanelDraft()"><i class="fa-solid fa-file-arrow-down"></i> Export Draft</button>
                        <button class="btn" onclick="document.getElementById('panel-draft-import').click()"><i class="fa-solid fa-file-arrow-up"></i> Import Draft</button>
                        <input type="file" id="panel-draft-import" accept=".json,application/json" style="display:none;" onchange="window.importPanelDraftFromFile(event)">
                        <button class="btn btn-primary" onclick="window.saveAdvancedRules()"><i class="fa-solid fa-check-double"></i> Save Settings</button>
                    </div>
                </div>
                
                <div class="grid-3">
                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-route text-accent"></i> Global Routing</div></div>
                        <div class="panel-body">
                            <div class="settings-row"><div class="settings-info" style="width:50%;"><h4>Domain Strategy</h4></div><div style="width:50%;"><select class="form-control" id="adv-domain-strategy"><option value="AsIs">AsIs</option><option value="IPIfNonMatch">IPIfNonMatch</option><option value="UseIP" selected>UseIP</option><option value="IPOnDemand">IPOnDemand</option></select></div></div>
                            <div class="settings-row"><div class="settings-info"><h4>Deep Packet Sniffing</h4></div><label class="switch"><input type="checkbox" id="adv-deep-sniff" checked><span class="slider"></span></label></div>
                            <div class="settings-row" style="flex-direction:column; align-items:flex-start; gap:12px; border:none; padding-bottom: 20px;">
                                <div class="settings-info"><h4>Sniffing Overrides</h4></div>
                                <div class="checkbox-grid">
                                    <label class="custom-checkbox"><input type="checkbox" id="adv-sniff-http" checked> HTTP</label>
                                    <label class="custom-checkbox"><input type="checkbox" id="adv-sniff-tls" checked> TLS</label>
                                    <label class="custom-checkbox"><input type="checkbox" id="adv-sniff-quic" checked> QUIC</label>
                                    <label class="custom-checkbox"><input type="checkbox" id="adv-sniff-fakedns"> fakedns</label>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-globe text-info"></i> Bypass Country Rules</div></div>
                        <div class="panel-body">
                            <div class="checkbox-grid" style="grid-template-columns: 1fr; gap: 16px;">
                                <label class="custom-checkbox"><input type="checkbox" id="adv-bypass-ir"> GeoIP: IR (Iran)</label>
                                <label class="custom-checkbox"><input type="checkbox" id="adv-bypass-ru"> GeoIP: RU (Russia)</label>
                                <label class="custom-checkbox"><input type="checkbox" id="adv-bypass-cn"> GeoIP: CN (China)</label>
                                <label class="custom-checkbox"><input type="checkbox" id="adv-bypass-lan"> LAN / Private IPs</label>
                            </div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-network-wired text-purple"></i> Advanced Core DNS</div></div>
                        <div class="panel-body">
                            <div class="settings-row"><div class="settings-info" style="width:40%;"><h4>Primary</h4></div><div style="width:60%;"><input type="text" class="form-control mono" id="adv-dns-primary" value="1.1.1.1"></div></div>
                            <div class="settings-row"><div class="settings-info" style="width:40%;"><h4>Fallback</h4></div><div style="width:60%;"><input type="text" class="form-control mono" id="adv-dns-fallback" value="8.8.8.8"></div></div>
                            <div class="settings-row" style="border:none; padding-bottom: 20px;"><div class="settings-info"><h4>DNS Cache</h4></div><label class="switch"><input type="checkbox" id="adv-dns-cache" checked><span class="slider"></span></label></div>
                        </div>
                    </div>
                </div>

                <div class="grid-2">
                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-layer-group text-accent"></i> Multiplexing</div></div>
                        <div class="panel-body">
                            <div class="settings-row"><div class="settings-info"><h4>Enable MUX</h4></div><label class="switch"><input type="checkbox" id="adv-mux-en"><span class="slider"></span></label></div>
                            <div class="settings-row"><div class="settings-info" style="width:50%;"><h4>Concurrency</h4></div><div style="width:50%;"><input type="number" class="form-control" id="adv-mux-concurrency" value="8"></div></div>
                            <div class="settings-row" style="border:none; padding-bottom: 20px;"><div class="settings-info"><h4>TLS Fragment</h4></div><select class="form-control" id="adv-tls-fragment" style="width: 140px;"><option value="none">Disabled</option><option value="10-20">10-20 bytes</option><option value="100-200">100-200 bytes</option></select></div>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-sliders text-purple"></i> System & API</div></div>
                        <div class="panel-body">
                            <div class="settings-row"><div class="settings-info"><h4>Log Level</h4></div><select class="form-control" id="adv-log-level" style="width:140px;"><option value="none">none</option><option value="warning" selected>warning</option><option value="info">info</option><option value="debug">debug</option></select></div>
                            <div class="settings-row" style="border:none; padding-bottom: 20px;"><div class="settings-info"><h4>Access Log</h4></div><label class="switch"><input type="checkbox" id="adv-access-log"><span class="slider"></span></label></div>
                        </div>
                    </div>
                </div>

                <div class="panel" style="flex-shrink: 0;">
                    <div class="panel-header"><div class="panel-title"><i class="fa-solid fa-file-code text-info"></i> Xray Config Preview</div><button class="btn" style="padding:6px 14px; font-size:0.75rem;" onclick="window.refreshConfigPreview()"><i class="fa-solid fa-rotate"></i> Refresh</button></div>
                    <div class="panel-body" style="gap:12px;"><textarea class="form-control mono" id="config-preview-json" style="min-height:300px; resize:vertical; background: #050505; color: #a1a1aa; border-color: var(--border);" readonly></textarea></div>
                </div>
            </div>

            <div id="tab-logs" class="tab-view">
                <div class="header-section">
                    <div><h2>Console Logs</h2><p>Panel messages or server audit trail.</p></div>
                    <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
                        <input type="text" id="log-search" class="form-control" placeholder="Search logs..." style="width:180px; padding:8px 14px; font-size:0.8rem;" oninput="window.filterLogs()">
                        <label class="switch" title="Auto-scroll"><input type="checkbox" id="log-autoscroll" checked><span class="slider"></span></label><span style="font-size:0.75rem; color:var(--text-muted); font-weight: 600;">Auto</span>
                        <button class="btn btn-danger" id="btn-clear-logs" onclick="window.setXrayStatus('clear_logs')"><i class="fa-solid fa-trash"></i> Clear</button>
                    </div>
                </div>
                <div class="panel panel-full" style="border:none;"><div class="terminal" id="log-output"></div></div>
            </div>

        </div>
    </main>

    <div class="modal-overlay" id="modal-confirm">
        <div class="modal" style="max-width: 420px;">
            <div class="modal-header"><div class="panel-title text-warning"><i class="fa-solid fa-triangle-exclamation"></i> Action Required</div></div>
            <div class="modal-body" id="confirm-msg" style="font-size: 0.95rem; line-height: 1.6; font-weight: 500; text-align: center;"></div>
            <div class="modal-footer" style="justify-content: center; gap: 16px;">
                <button class="btn" onclick="closeConfirm(false)" style="min-width: 100px;">Cancel</button>
                <button class="btn btn-danger" onclick="closeConfirm(true)" style="min-width: 100px;">Proceed</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="modal-client">
        <div class="modal" style="max-width: 580px;">
            <div class="modal-header"><div class="panel-title">Client Profile</div><button class="btn-icon" style="background:none; border:none; color:var(--text-muted);" onclick="closeModal('modal-client')"><i class="fa-solid fa-times fa-lg"></i></button></div>
            <div class="modal-body">
                <input type="hidden" id="c-edit-id" value="">
                <p id="c-donate-msg" style="display:none; color:var(--purple); font-size:0.8rem; margin-bottom:16px; font-weight:600;"><i class="fa-solid fa-hand-holding-heart"></i> Community config. Advanced settings locked.</p>
                <div class="form-grid">
                    <div class="form-group"><label class="form-label">Remarks</label><input type="text" class="form-control" id="c-name" placeholder="Enter Remark ..."></div>
                    <div class="form-group"><label class="form-label">UUID</label><div class="input-group"><input type="text" class="form-control mono" id="c-uuid"><button class="btn btn-icon" id="btn-gen-uuid" onclick="document.getElementById('c-uuid').value = genUUID()" style="height: auto;"><i class="fa-solid fa-rotate-right"></i></button></div></div>
                    <div class="form-group"><label class="form-label">Expiry Date</label><input type="datetime-local" class="form-control" id="c-expiry"></div>
                    <div class="form-group"><label class="form-label">Limit (GB) [0 = Unlim]</label><input type="number" class="form-control" id="c-limit" value="0"></div>
                    <div class="form-group" style="grid-column: 1/-1;"><label class="form-label">uTLS Fingerprint</label><select class="form-control" id="c-utls"><option value="chrome">Chrome</option><option value="firefox">Firefox</option><option value="safari">Safari</option><option value="random">Random</option></select></div>
                </div>
                <div class="settings-row" style="margin-top:20px; padding:16px 0 0 0; border-top:1px solid var(--border); border-bottom:none;">
                    <div class="settings-info"><h4>Client Active</h4><p>Enable or disable this client.</p></div>
                    <label class="switch"><input type="checkbox" id="c-active" checked><span class="slider"></span></label>
                </div>
            </div>
            <div class="modal-footer"><button class="btn" onclick="closeModal('modal-client')">Cancel</button><button class="btn btn-primary" id="btn-save-client" onclick="window.saveClient()"><i class="fa-solid fa-check"></i> Save Client</button></div>
        </div>
    </div>

    <div class="modal-overlay" id="modal-donate">
        <div class="modal" style="max-width: 440px;">
            <div class="modal-header"><div class="panel-title">Donate Config</div><button class="btn-icon" style="background:none; border:none; color:var(--text-muted);" onclick="closeModal('modal-donate')"><i class="fa-solid fa-times fa-lg"></i></button></div>
            <div class="modal-body" style="display:flex; flex-direction:column; gap:16px;">
                <p style="color:var(--text-muted); font-size:0.85rem; line-height:1.5;">Creates a specialized client pushed to the community pool.</p>
                <div class="form-group"><label class="form-label">Usage Limit (GB)</label><input type="number" class="form-control" id="don-limit" value="50" min="1"></div>
                <div class="form-group"><label class="form-label">Active Duration (Days)</label><input type="number" class="form-control" id="don-days" value="7" min="1"></div>
            </div>
            <div class="modal-footer"><button class="btn" onclick="closeModal('modal-donate')">Cancel</button><button class="btn" style="background:var(--purple); color:#fff; border:none;" onclick="window.submitDonate()"><i class="fa-solid fa-paper-plane"></i> Provision</button></div>
        </div>
    </div>

    <div class="modal-overlay" id="modal-qr">
        <div class="modal" style="max-width: 420px; text-align:center;">
            <div class="modal-header"><div class="panel-title">QR Connect</div><button class="btn-icon" style="background:none; border:none; color:var(--text-muted);" onclick="closeModal('modal-qr')"><i class="fa-solid fa-times fa-lg"></i></button></div>
            <div class="modal-body" style="display:flex; flex-direction:column; align-items:center;">
                <div class="qr-wrapper" id="qrcode"></div>
                <textarea class="form-control mono" id="qr-text" rows="4" style="margin-top:20px; resize:none; font-size:0.75rem; width:100%;" readonly></textarea>
            </div>
            <div class="modal-footer" style="justify-content:center;">
                <button class="btn btn-primary" onclick="copyToClipboard(document.getElementById('qr-text').value); showToast('Copied to clipboard!', 'success');" style="width:100%;"><i class="fa-solid fa-copy"></i> Copy Link</button>
            </div>
        </div>
    </div>

    <div class="toast-box" id="toaster"></div>

    <script>
        const passSetup = true;
        const loggedIn = true;

        window.doLogin = function() {
            const p = document.getElementById('pass-input').value;
            if(!p) return showToast('Enter a password', 'error');
            fetch('/api/login', { method:'POST', body:JSON.stringify({pass: p}) })
                .then(r=>r.json()).then(d=>{
                if(d.ok) { document.getElementById('loader').style.opacity = '1'; document.getElementById('loader').style.visibility = 'visible'; setTimeout(() => location.reload(), 300); } 
                else { showToast('Incorrect password', 'error'); }
            }).catch(()=>showToast('Network error', 'error'));
        };

        Chart.defaults.color = '#a1a1aa'; Chart.defaults.font.family = "'Plus Jakarta Sans', sans-serif"; Chart.defaults.font.size = 12;

        let trafficChart, hwChart, clientPieChart, clientFlowChart;
        window.clients = []; 
        window.subEntries = []; window.subClientSubscriptions = {};
        window.lastTelemetry = {}; window.PORT_DOMAIN = '';
        
        const backendSync = { connected: false, syncing: false, debounceHandle: null };

        let confirmCallback = null;
        window.customConfirm = function(msg, cb) {
            document.getElementById('confirm-msg').innerText = msg;
            confirmCallback = cb;
            openModal('modal-confirm');
        };
        window.closeConfirm = function(res) {
            closeModal('modal-confirm');
            if(confirmCallback) confirmCallback(res);
        };

        function switchTab(tabId) {
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            const trigger = (typeof event !== 'undefined' && event?.currentTarget) ? event.currentTarget : document.querySelector(`.nav-item[onclick*="switchTab('${tabId}')"]`);
            if(trigger) trigger.classList.add('active');
            document.querySelectorAll('.tab-view').forEach(e => e.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            
            const topbar = document.getElementById('main-topbar');
            const mobileBtn = document.getElementById('mobile-dash-btn');
            if (tabId === 'dashboard') {
                topbar.classList.add('hidden');
                mobileBtn.style.display = window.innerWidth <= 1024 ? 'block' : 'none';
            } else {
                topbar.classList.remove('hidden');
                mobileBtn.style.display = 'none';
            }
            if(window.innerWidth <= 1024) document.getElementById('sidebar').classList.remove('open');
        }

        function resetModalTabs(modalId) {
            const modal = document.getElementById(modalId);
            if(modal) {
                const firstBtn = modal.querySelector('.modal-tab-btn');
                const firstContent = modal.querySelector('.modal-tab-content');
                if(firstBtn && firstContent) {
                    modal.querySelectorAll('.modal-tab-btn').forEach(b => b.classList.remove('active'));
                    modal.querySelectorAll('.modal-tab-content').forEach(c => c.classList.remove('active'));
                    firstBtn.classList.add('active');
                    firstContent.classList.add('active');
                }
            }
        }

        function switchModalTab(btn, contentId) {
            const modal = btn.closest('.modal');
            modal.querySelectorAll('.modal-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            modal.querySelectorAll('.modal-tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById(contentId).classList.add('active');
        }

        function openModal(id) { document.getElementById(id).classList.add('show'); }
        function closeModal(id) { document.getElementById(id).classList.remove('show'); }
        
        function showToast(msg, type='info') {
            const toaster = document.getElementById('toaster');
            const el = document.createElement('div'); el.className = 'toast';
            el.innerHTML = `<i class="fa-solid ${type==='success'?'fa-check-circle text-accent':(type==='error'?'fa-circle-xmark text-danger':'fa-info-circle text-info')}"></i> <span>${msg}</span>`;
            toaster.appendChild(el);
            setTimeout(() => { el.style.opacity='0'; el.style.transform='translateY(10px)'; setTimeout(() => el.remove(), 250); }, 2500);
        }

        function genUUID() { return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { let r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8); return v.toString(16); }); }

        function copyToClipboard(text) {
            if (navigator.clipboard && window.isSecureContext) { return navigator.clipboard.writeText(text).catch(() => _clipboardFallback(text)); }
            _clipboardFallback(text); return Promise.resolve();
        }
        function _clipboardFallback(text) {
            const ta = document.createElement('textarea'); ta.value = text;
            ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0;';
            document.body.appendChild(ta); ta.focus(); ta.select();
            try { document.execCommand('copy'); } catch (_) {}
            document.body.removeChild(ta);
        }
        window.copyPlaceholder = function(text) { copyToClipboard(text); showToast('Copied: ' + text, 'success'); };
        
        function togglePanel(header) {
            const body = header.nextElementSibling; const icon = header.querySelector('.collapse-icon');
            if(body) body.classList.toggle('collapsed'); if(icon) icon.classList.toggle('collapsed');
        }

        function ensureCharts() {
            if(!trafficChart) {
                trafficChart = new Chart(document.getElementById('chart-traffic').getContext('2d'), {
                    type: 'line', data: { labels: Array(60).fill(''), datasets: [
                        { label: 'Speed DL (Mbps)', data: Array(60).fill(0), borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.08)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 },
                        { label: 'Speed UL (Mbps)', data: Array(60).fill(0), borderColor: '#3b82f6', backgroundColor: 'rgba(59, 130, 246, 0.08)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 }
                    ]}, options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: false }, y: { beginAtZero: true } } }
                });
            }
            if(!hwChart) {
                hwChart = new Chart(document.getElementById('chart-hardware').getContext('2d'), {
                    type: 'line', data: { labels: Array(60).fill(''), datasets: [
                        { label: 'CPU %', yAxisID: 'y', data: Array(60).fill(0), borderColor: '#f59e0b', borderWidth: 2, pointRadius: 0, tension: 0.4 },
                        { label: 'RAM MB', yAxisID: 'y1', data: Array(60).fill(0), borderColor: '#8b5cf6', backgroundColor: 'rgba(139, 92, 246, 0.08)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 }
                    ]}, options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: false }, y: { type: 'linear', display: false, min: 0, max: 100 }, y1: { type: 'linear', position: 'right', min: 0, max: 4096 } } }
                });
            }
            if(!clientPieChart) {
                clientPieChart = new Chart(document.getElementById('client-pie-chart').getContext('2d'), {
                    type: 'doughnut', data: { labels: [], datasets: [{ data: [], backgroundColor: ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#ef4444', '#ec4899'], borderWidth: 0 }] },
                    options: { responsive: true, maintainAspectRatio: false, cutout: '75%', plugins: { legend: { position: 'right', labels: { color: '#a1a1aa', usePointStyle: true, boxWidth: 6 } } } }
                });
            }
            if(!clientFlowChart) {
                clientFlowChart = new Chart(document.getElementById('client-flow-chart').getContext('2d'), {
                    type: 'line', data: { labels: Array(30).fill(''), datasets: [
                        { label: 'DL (Mbps)', data: Array(30).fill(0), borderColor: '#8b5cf6', backgroundColor: 'rgba(139, 92, 246, 0.1)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 },
                        { label: 'UL (Mbps)', data: Array(30).fill(0), borderColor: '#f59e0b', backgroundColor: 'rgba(245, 158, 11, 0.1)', borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 }
                    ]}, options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, display: false }, x: { display: false } } }
                });
            }
        }

        window.logEntries = [];
        window.logCore = function(msg) {
            const time = new Date().toLocaleTimeString('en-US', { hour12: false });
            window.logEntries.push({ time, msg });
            if(window.logEntries.length > 200) window.logEntries.shift();
            window.filterLogs();
        };

        window.filterLogs = function() {
            const term = document.getElementById('log-output');
            if (!term) return;
            const search = (document.getElementById('log-search')?.value || '').toLowerCase();
            term.innerHTML = '';
            window.logEntries.filter(e => {
                if (search && !e.msg.toLowerCase().includes(search) && !e.time.includes(search)) return false;
                return true;
            }).forEach(e => {
                let color = 'var(--text-main)';
                if (e.msg.includes('[WARN]') || e.msg.includes('warning')) color = 'var(--warning)';
                if (e.msg.includes('[error]') || e.msg.includes('[ERROR]')) color = 'var(--danger)';
                if (e.msg.includes('[INFO]') || e.msg.includes('info')) color = 'var(--accent)';
                const row = document.createElement('div');
                row.style.cssText = `color:${color}; margin-bottom:6px;`;
                row.innerHTML = `<span style="color:var(--text-muted); margin-right:10px;">[${e.time}]</span> ${e.msg}`;
                term.appendChild(row);
            });
            const autoScroll = document.getElementById('log-autoscroll');
            if (!autoScroll || autoScroll.checked) term.scrollTop = term.scrollHeight;
        };

        window.applyPanelState = function(state, dataPayload) {
            if(!state) return;
            if(dataPayload && dataPayload.portDomain) window.PORT_DOMAIN = dataPayload.portDomain;
            if(Array.isArray(state.clients)) clients = state.clients;
            if(state.subClientSubscriptions) subClientSubscriptions = state.subClientSubscriptions;
            
            if(state.settings) {
                document.getElementById('wake-lock-toggle').checked = state.settings.wakeLock || false;
                document.getElementById('quota-dollars').value = state.settings.quotaBalance || 0;
                
                let adv = state.settings.advanced || {};
                document.getElementById('adv-domain-strategy').value = adv.domainStrategy || 'UseIP';
                document.getElementById('adv-deep-sniff').checked = adv.deepSniff !== false;
                document.getElementById('adv-sniff-http').checked = adv.sniffHttp !== false;
                document.getElementById('adv-sniff-tls').checked = adv.sniffTls !== false;
                document.getElementById('adv-sniff-quic').checked = adv.sniffQuic !== false;
                document.getElementById('adv-sniff-fakedns').checked = adv.sniffFakedns || false;
                document.getElementById('adv-bypass-ir').checked = adv.bypassIr || false;
                document.getElementById('adv-bypass-ru').checked = adv.bypassRu || false;
                document.getElementById('adv-bypass-cn').checked = adv.bypassCn || false;
                document.getElementById('adv-bypass-lan').checked = adv.bypassLan || false;
                document.getElementById('adv-dns-primary').value = adv.dnsPrimary || '1.1.1.1';
                document.getElementById('adv-dns-fallback').value = adv.dnsFallback || '8.8.8.8';
                document.getElementById('adv-dns-cache').checked = adv.dnsCache !== false;
                document.getElementById('adv-mux-en').checked = adv.mux || false;
                document.getElementById('adv-mux-concurrency').value = adv.muxConcurrency || 8;
                document.getElementById('adv-tls-fragment').value = adv.tlsFragment || 'none';
                document.getElementById('adv-log-level').value = adv.logLevel || 'warning';
                document.getElementById('adv-access-log').checked = adv.accessLog || false;
            }
            
            renderClients();
            populateSubClientSelect(); refreshConfigPreview();
        };

        window.updateTelemetryFromBackend = function(t) {
            window.lastTelemetry = t;
            const xrayUp = t.xrayRunning === true;
            document.getElementById('dash-status-tag').className = xrayUp ? 'tag tag-green' : 'tag tag-red';
            document.getElementById('dash-status-tag').innerText = xrayUp ? 'Online' : 'Offline';
            document.getElementById('topbar-xray-dot').style.background = xrayUp ? 'var(--accent)' : 'var(--danger)';
            document.getElementById('topbar-xray-dot').style.boxShadow = xrayUp ? '0 0 8px var(--accent)' : 'none';
            document.getElementById('topbar-xray-label').style.color = xrayUp ? 'var(--accent)' : 'var(--danger)';
            document.getElementById('topbar-xray-label').innerText = xrayUp ? 'Xray ON' : 'Xray OFF';
            
            document.getElementById('m-rx').innerHTML = `${Number(t.totalRxGb||0).toFixed(2)} <span class="metric-sub">GB</span>`;
            document.getElementById('m-tx').innerHTML = `${Number(t.totalTxGb||0).toFixed(2)} <span class="metric-sub">GB</span>`;
            document.getElementById('m-rx-mini').innerText = Number(t.totalRxGb||0).toFixed(2);
            document.getElementById('m-tx-mini').innerText = Number(t.totalTxGb||0).toFixed(2);
            
            document.getElementById('m-speed').innerHTML = `${Number(t.speedDownMbps||0).toFixed(1)} <span class="metric-sub">/ ${Number(t.speedUpMbps||0).toFixed(1)} Mbps</span>`;
            document.getElementById('m-speed-mini').innerText = `${Number(t.speedDownMbps||0).toFixed(1)} / ${Number(t.speedUpMbps||0).toFixed(1)}`;
            
            let s = t.xrayUptimeSec || 0;
            document.getElementById('m-uptime').innerText = xrayUp ? `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60).toString().padStart(2,'0')}m` : 'Stopped';
            
            const ramTotal = Number(t.ramTotalMb || 4096);
            document.getElementById('dash-load-avg').innerText = t.loadAvg.map(x=>x.toFixed(2)).join(' / ');
            document.getElementById('dash-mem-alloc').innerText = `${Number(t.ramMb||0).toFixed(2)} / ${Number(t.ramTotalMb||4096).toFixed(2)} MB`;
            
            document.getElementById('hw-cpu-val').innerText = `${Number(t.cpuPct||0).toFixed(1)}%`;
            document.getElementById('hw-cpu-bar').style.width = `${Math.min(100, Number(t.cpuPct||0))}%`;
            document.getElementById('hw-ram-val').innerText = `${Number(t.ramMb||0).toFixed(2)} MB`;
            document.getElementById('hw-ram-bar').style.width = `${Math.min(100, (Number(t.ramMb||0)/ramTotal)*100)}%`;
            document.getElementById('hw-disk-val').innerText = `${t.diskUsedGb.toFixed(1)} / ${t.diskTotalGb.toFixed(1)} GB`;
            document.getElementById('hw-disk-bar').style.width = t.diskTotalGb ? `${Math.min(100, (t.diskUsedGb/t.diskTotalGb)*100)}%` : '0%';
            
            document.getElementById('q-used').innerText = `Used: ${Number(t.quotaUsedH||0).toFixed(1)}h`;
            document.getElementById('q-rem').innerText = `Remaining: ${Number(t.quotaRemainH||0).toFixed(1)}h`;
            document.getElementById('q-base-hours').innerText = '60h';
            document.getElementById('q-funded-hours').innerText = `${(parseFloat(document.getElementById('quota-dollars').value)||0)*20}h`;
            document.getElementById('q-bar').style.width = t.quotaTotalH ? `${Math.min(100, (t.quotaUsedH/t.quotaTotalH)*100)}%` : '0%';

            document.getElementById('cs-city').innerText = t.ipCity || 'N/A';
            document.getElementById('cs-country').innerText = t.ipCountry || 'N/A';
            document.getElementById('cs-ipv4').innerText = t.ipIpv4 || 'N/A';
            document.getElementById('cs-provider').innerText = 'GitHubCodeSpace';

            if(trafficChart) {
                trafficChart.data.datasets[0].data.push(t.speedDownMbps || 0);
                trafficChart.data.datasets[1].data.push(t.speedUpMbps || 0);
                if(trafficChart.data.datasets[0].data.length > 60) {
                    trafficChart.data.datasets[0].data.shift();
                    trafficChart.data.datasets[1].data.shift();
                }
                trafficChart.update('none');
            }
            if(hwChart) {
                hwChart.data.datasets[0].data.push(t.cpuPct); hwChart.data.datasets[1].data.push(t.ramMb);
                if(hwChart.data.datasets[0].data.length > 60) { hwChart.data.datasets[0].data.shift(); hwChart.data.datasets[1].data.shift(); }
                hwChart.update('none');
            }
            if(clientPieChart) {
                clientPieChart.data.labels = clients.map(c => c.name);
                clientPieChart.data.datasets[0].data = clients.map(c => c.usage || 0);
                clientPieChart.update();
            }
            if(clientFlowChart) {
                clientFlowChart.data.datasets[0].data.push(t.speedDownMbps || 0);
                clientFlowChart.data.datasets[1].data.push(t.speedUpMbps || 0);
                if(clientFlowChart.data.datasets[0].data.length > 30) {
                    clientFlowChart.data.datasets[0].data.shift();
                    clientFlowChart.data.datasets[1].data.shift();
                }
                clientFlowChart.update('none');
            }
            
            renderClients();
        };

        window.serializePanelState = function() {
            return {
                clients, subClientSubscriptions,
                settings: {
                    wakeLock: document.getElementById('wake-lock-toggle').checked,
                    quotaBalance: parseFloat(document.getElementById('quota-dollars').value) || 0,
                    advanced: {
                        domainStrategy: document.getElementById('adv-domain-strategy').value,
                        deepSniff: document.getElementById('adv-deep-sniff').checked,
                        sniffHttp: document.getElementById('adv-sniff-http').checked,
                        sniffTls: document.getElementById('adv-sniff-tls').checked,
                        sniffQuic: document.getElementById('adv-sniff-quic').checked,
                        sniffFakedns: document.getElementById('adv-sniff-fakedns').checked,
                        bypassIr: document.getElementById('adv-bypass-ir').checked,
                        bypassRu: document.getElementById('adv-bypass-ru').checked,
                        bypassCn: document.getElementById('adv-bypass-cn').checked,
                        bypassLan: document.getElementById('adv-bypass-lan').checked,
                        dnsPrimary: document.getElementById('adv-dns-primary').value,
                        dnsFallback: document.getElementById('adv-dns-fallback').value,
                        dnsCache: document.getElementById('adv-dns-cache').checked,
                        mux: document.getElementById('adv-mux-en').checked,
                        muxConcurrency: parseInt(document.getElementById('adv-mux-concurrency').value) || 8,
                        tlsFragment: document.getElementById('adv-tls-fragment').value,
                        logLevel: document.getElementById('adv-log-level').value,
                        accessLog: document.getElementById('adv-access-log').checked
                    }
                }
            };
        };

        window.schedulePanelSync = function(reason='change') {
            if(backendSync.debounceHandle) clearTimeout(backendSync.debounceHandle);
            backendSync.debounceHandle = setTimeout(() => pushPanelState(reason), 300);
        };

        window.pushPanelState = async function(reason='sync') {
            if(backendSync.syncing || !backendSync.connected) return;
            backendSync.syncing = true;
            try {
                const res = await fetch('/api/state', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ state: serializePanelState(), reason })
                });
                const data = await res.json();
                if(!data.ok) throw new Error(data.error || 'state sync failed');
            } catch (err) { showToast(`Backend sync failed: ${err.message || err}`, 'error'); } 
            finally { backendSync.syncing = false; }
        };

        window.toggleWakeLock = function(el) {
            if(el.checked) {
                el.checked = false; 
                window.customConfirm("Use at your own risk. Your account might get suspended. Proceed?", (res) => {
                    if(res) {
                        el.checked = true;
                        showToast('Wake Lock Enabled.', 'warning');
                        window.schedulePanelSync('wakeLock');
                    }
                });
            } else {
                showToast('Wake Lock Disabled.', 'info');
                window.schedulePanelSync('wakeLock');
            }
        };

        function renderClients() {
            const tb = document.querySelector('#tbl-clients tbody');
            if(!tb) return;
            const scrollPos = tb.parentElement.scrollTop;
            tb.innerHTML = '';
            clients.forEach(c => {
                tb.innerHTML += `<tr>
                    <td><div style="font-weight:700;">${c.name}</div><div class="mono" style="font-size:0.65rem; color:var(--text-muted);">${c.id.split('-')[0]}...</div></td>
                    <td><span class="tag tag-blue">${c.utls||'chrome'}</span></td>
                    <td><strong>${(c.usage||0).toFixed(2)}</strong> <span style="font-size:0.75rem; color:var(--text-muted);">/ ${c.limit===0?'∞':c.limit+' GB'}</span></td>
                    <td><span class="mono" style="color:var(--text-muted);">${c.expiry?new Date(c.expiry).toLocaleString():'Never'}</span></td>
                    <td><span class="tag ${c.status?'tag-green':'tag-red'}">${c.status?'Active':'Disabled'}</span></td>
                    <td style="text-align:right;">
                        <button class="btn btn-icon" onclick="window.showQR('${c.id}')"><i class="fa-solid fa-qrcode"></i></button>
                        <button class="btn btn-icon" onclick="window.copyClientSubscriptionLink('${c.id}')"><i class="fa-solid fa-link"></i></button>
                        <button class="btn btn-icon" onclick="window.openEditClient('${c.id}')"><i class="fa-solid fa-pen"></i></button>
                        <button class="btn btn-icon btn-danger" onclick="window.deleteClient('${c.id}')"><i class="fa-solid fa-trash"></i></button>
                    </td>
                </tr>`;
            });
            tb.parentElement.scrollTop = scrollPos;
            populateSubClientSelect();
        }

        window.openAddClientModal = function() {
            document.getElementById('c-edit-id').value=''; document.getElementById('c-name').value=''; document.getElementById('c-uuid').value=genUUID();
            document.getElementById('c-limit').value=0; document.getElementById('c-expiry').value=''; document.getElementById('c-active').checked=true;
            openModal('modal-client');
        }
        window.openEditClient = function(id) {
            const c = clients.find(x => x.id === id); if(!c) return;
            document.getElementById('c-edit-id').value = c.id; document.getElementById('c-name').value = c.name;
            document.getElementById('c-uuid').value = c.id; document.getElementById('c-utls').value = c.utls || 'chrome';
            document.getElementById('c-limit').value = c.limit || 0; document.getElementById('c-expiry').value = c.expiry || '';
            document.getElementById('c-active').checked = !!c.status;
            openModal('modal-client');
        }
        window.saveClient = function() {
            const id = document.getElementById('c-edit-id').value, name = document.getElementById('c-name').value;
            if(!name) return showToast('Name required', 'error');
            const data = {
                name, limit: parseFloat(document.getElementById('c-limit').value)||0,
                expiry: document.getElementById('c-expiry').value ? new Date(document.getElementById('c-expiry').value).toISOString() : '',
                status: document.getElementById('c-active').checked ? 1 : 0,
                utls: document.getElementById('c-utls').value
            };
            if(id) { const c = clients.find(x => x.id === id); if(c) Object.assign(c, data); }
            else { clients.push({ id: document.getElementById('c-uuid').value, usage: 0, ...data }); }
            renderClients(); pushPanelState('saveClient'); closeModal('modal-client'); showToast('Saved', 'success');
        }
        window.deleteClient = function(id) {
            window.customConfirm("Are you sure you want to delete this client?", (res) => {
                if(res) {
                    clients = clients.filter(c => c.id !== id); renderClients(); pushPanelState('deleteClient'); showToast('Removed', 'success');
                }
            });
        }
        window.openDonateModal = function() { openModal('modal-donate'); }
        window.submitDonate = async function() {
            const donName = 'Code-Leafy🍃 | ' + (window.lastTelemetry.githubUser || 'Code-Leafy');
            const clientId = genUUID();
            clients.push({ id: clientId, name: donName, utls: 'chrome', usage: 0, limit: parseFloat(document.getElementById('don-limit').value)||50, expiry: '', status: 1 });
            renderClients(); pushPanelState('submitDonate'); closeModal('modal-donate');
            try {
                const res = await fetch('/api/donate', { method: 'POST' });
                const data = await res.json();
                if(data.ok) showToast('Config donated!', 'success');
            } catch(e) {}
        }

        async function getSubscriptionLink(clientId) {
            try {
                const res = await fetch(`/api/sub/link/${encodeURIComponent(clientId)}`);
                if(res.status === 401) return location.reload();
                const data = await res.json();
                if(data.ok && data.link) return data.link;
            } catch(e) {} return null;
        }
        window.copyClientSubscriptionLink = async function(clientId) {
            const link = await getSubscriptionLink(clientId);
            if(!link) return showToast('Error generating link.', 'error');
            copyToClipboard(link); showToast('Subscription link copied!', 'success');
        }
        window.showQR = async function(id) {
            const link = await getSubscriptionLink(id);
            if(!link) return showToast('Error generating link.', 'error');
            document.getElementById('qr-text').value = link; document.getElementById('qrcode').innerHTML = '';
            new QRCode(document.getElementById("qrcode"), { text: link, width: 240, height: 240, correctLevel : QRCode.CorrectLevel.M });
            openModal('modal-qr');
        }

        window.resolvePlaceholders = function(text, client) {
            if(!text) return "";
            let t = text;
            t = t.replace(/%client-name%/g, client ? client.name : "");
            t = t.replace(/%data-used%/g, client ? (client.usage || 0).toFixed(2) : "0.00");
            t = t.replace(/%data-total%/g, (client && client.limit) ? client.limit.toFixed(2) : "∞");
            let tel = window.lastTelemetry || {};
            t = t.replace(/%quota-used%/g, tel.quotaUsedH ? tel.quotaUsedH.toFixed(1) : "0.0");
            t = t.replace(/%quota-remain%/g, tel.quotaRemainH ? tel.quotaRemainH.toFixed(1) : "0.0");
            t = t.replace(/%quota-total%/g, tel.quotaTotalH ? tel.quotaTotalH.toFixed(1) : "0.0");
            t = t.replace(/%expiry-date%/g, client && client.expiry ? new Date(client.expiry).toLocaleDateString() : "Never");
            return t;
        };

        function populateSubClientSelect() {
            const sel = document.getElementById('sub-client'); if(!sel) return;
            const prev = sel.value; sel.innerHTML = '<option value="">— Select Client —</option>';
            clients.forEach(c => sel.innerHTML += `<option value="${c.id}">${c.name}</option>`);
            sel.value = prev;
        }
        window.onSubClientChange = function() {
            const clientId = document.getElementById('sub-client').value;
            if(clientId && subClientSubscriptions[clientId]) {
                subEntries = JSON.parse(JSON.stringify(subClientSubscriptions[clientId]));
            } else {
                subEntries = [];
            }
            window.renderSubEntries(); window.renderSubPreview();
        };

        window.addSubEntry = function(type) {
            const cId = document.getElementById('sub-client').value;
            if(!cId) return showToast('Select a client first', 'error');
            let nName = '';
            let transport = 'xhttp';
            if(type === 'proxy') {
                transport = document.getElementById('transport-sel').value;
                let pCnt = subEntries.filter(e => e.type === 'proxy').length;
                nName = `Code-Leafy🍃 ${pCnt + 1}`;
            } else {
                nName = 'Code-Leafy🍃 %data-used%GB / %data-total%GB | %quota-remain%h left';
            }
            subEntries.push({
                id: genUUID(), type: type, transport: transport,
                name: nName,
                ipAddress: window.PORT_DOMAIN || ''
            });
            window.renderSubEntries(); window.renderSubPreview();
        };

        window.removeSubEntry = function(id) {
            subEntries = subEntries.filter(e => e.id !== id);
            window.renderSubEntries(); window.renderSubPreview();
        };

        window.updateSubEntry = function(id, field, value) {
            const entry = subEntries.find(e => e.id === id);
            if(entry) entry[field] = value;
            window.renderSubPreview();
        };

        let dragSrcIndex = null;
        window.dragSubEntry = function(e, index) { dragSrcIndex = index; };
        window.dropSubEntry = function(e, index) {
            e.preventDefault();
            if(dragSrcIndex === null || dragSrcIndex === index) return;
            const item = subEntries.splice(dragSrcIndex, 1)[0];
            subEntries.splice(index, 0, item);
            window.renderSubEntries(); window.renderSubPreview();
        };

        window.renderSubEntries = function() {
            const list = document.getElementById('sub-entries-list');
            const count = document.getElementById('sub-entry-count');
            if(!list) return;
            if(subEntries.length === 0) {
                list.innerHTML = '<div id="sub-empty-hint" style="color:var(--text-muted); font-size:0.85rem; text-align:center; padding:30px 0;">Select a client and click <strong>+ Proxy</strong> or <strong>Info</strong>.</div>';
                if(count) count.innerText = '0';
                return;
            }
            if(count) count.innerText = subEntries.length;
            let html = '';
            subEntries.forEach((entry, i) => {
                html += `<div class="sub-entry" draggable="true" ondragstart="window.dragSubEntry(event, ${i})" ondragover="event.preventDefault()" ondrop="window.dropSubEntry(event, ${i})">
                    <div class="sub-entry-drag"><i class="fa-solid fa-grip-vertical"></i></div>
                    <div class="sub-entry-body">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span class="sub-entry-type" style="color:${entry.type==='proxy'?'var(--accent)':'var(--info)'}">${entry.type.toUpperCase()}</span>
                            <i class="fa-solid fa-times" style="cursor:pointer; color:var(--danger); padding:4px;" onclick="window.removeSubEntry('${entry.id}')"></i>
                        </div>
                        <input type="text" class="form-control" style="padding:8px 12px; font-size:0.8rem;" value="${entry.name}" oninput="window.updateSubEntry('${entry.id}', 'name', this.value)">
                        ${entry.type === 'proxy' 
                            ? `<div style="display:flex; gap:10px; margin-top:4px;">
                                   <input type="text" class="form-control" style="flex:1; padding:8px 12px; font-size:0.8rem;" placeholder="IP (Leave blank for default)" value="${entry.ipAddress || ''}" oninput="window.updateSubEntry('${entry.id}', 'ipAddress', this.value)">
                                   <span class="tag tag-purple" style="align-self:center;">${(entry.transport||'xhttp').toUpperCase()}</span>
                               </div>` 
                            : ``}
                    </div>
                </div>`;
            });
            list.innerHTML = html;
        };

        window.renderSubPreview = function() {
            const list = document.getElementById('phone-config-list');
            if(!list) return;
            const cId = document.getElementById('sub-client').value;
            const client = clients.find(c => c.id === cId);

            if(subEntries.length === 0) {
                list.innerHTML = '<div style="color:#52525b; font-size:0.75rem; text-align:center; padding:30px 0;">No configs yet</div>';
                return;
            }
            let html = '';
            subEntries.forEach(entry => {
                const icon = entry.type === 'proxy' ? '<i class="fa-solid fa-shield-halved" style="color:var(--accent)"></i>' : '<i class="fa-solid fa-circle-info" style="color:var(--info)"></i>';
                const sub = entry.type === 'proxy' ? (entry.ipAddress || 'Auto IP') + ` • ${(entry.transport||'xhttp').toUpperCase()}` : 'Info';
                const title = window.resolvePlaceholders(entry.name, client);
                html += `<div class="phone-item ${entry.type==='info'?'info-item':''}">
                    <div class="phone-item-icon">${icon}</div>
                    <div class="phone-item-body">
                        <div class="phone-item-name">${title}</div>
                        <div class="phone-item-sub">${sub}</div>
                    </div>
                    <div class="phone-item-action">
                        <button class="btn btn-icon" style="width:28px; height:28px; padding:0;" onclick="window.copySingleEntry('${entry.id}')"><i class="fa-solid fa-copy" style="font-size:0.7rem;"></i></button>
                    </div>
                </div>`;
            });
            list.innerHTML = html;
        };

        window.copySingleEntry = function(entryId) {
            const entry = subEntries.find(e => e.id === entryId);
            if(!entry) return;
            const cId = document.getElementById('sub-client').value;
            const client = clients.find(c => c.id === cId);
            
            let link = '';
            if(entry.type === 'proxy') {
                let ip = entry.ipAddress ? entry.ipAddress.trim() : window.PORT_DOMAIN;
                if(!ip) ip = window.PORT_DOMAIN;
                let name = encodeURIComponent(window.resolvePlaceholders(entry.name, client));
                let certSha256 = window.lastTelemetry.certSha256 || "";
                let certParam = certSha256 ? `&cert=${encodeURIComponent(certSha256)}` : "";
                let trans = entry.transport || 'xhttp';
                
                if(trans === 'ws') {
                    link = `vless://${client.id}@${ip}:443?encryption=none&security=tls&sni=${window.PORT_DOMAIN}&fp=chrome&alpn=h3,h2,http/1.1&type=ws&host=${window.PORT_DOMAIN}&path=%2Fws${certParam}#${name}`;
                } else {
                    link = `vless://${client.id}@${ip}:443?encryption=none&security=tls&sni=${window.PORT_DOMAIN}&fp=chrome&alpn=h3,h2,http/1.1&type=xhttp&host=${window.PORT_DOMAIN}&path=%2F${certParam}&mode=packet-up#${name}`;
                }
            } else {
                let text = encodeURIComponent(window.resolvePlaceholders(entry.name, client));
                link = `trojan://${genUUID()}@127.0.0.1:80?security=none#${text}`;
            }
            copyToClipboard(link);
            showToast('Config copied!', 'success');
        };

        window.saveSubscriptionForClient = function() {
            const clientId = document.getElementById('sub-client').value;
            if(!clientId) return showToast('Select a client first', 'error');
            subClientSubscriptions[clientId] = JSON.parse(JSON.stringify(subEntries));
            pushPanelState('saveSub');
            showToast('Subscription Layout Saved!', 'success');
        };

        window.saveAdvancedRules = function() { pushPanelState('saveAdvancedRules'); showToast('Settings Saved', 'success'); }

        window.refreshConfigPreview = function() {
            const adv = window.serializePanelState().settings.advanced || {};
            const cfg = {
                log: { level: adv.logLevel || 'warning', access: adv.accessLog ? "access.log" : "none", error: "xray.log" },
                inbounds: [
                    { tag: "vless-xhttp", port: 10001, protocol: "vless", streamSettings: { network: "xhttp", xhttpSettings: { mode: "packet-up", path: "/" }, sockopt: { tcpFastOpen: true, tcpNoDelay: true } } },
                    { tag: "vless-ws", port: 10003, protocol: "vless", streamSettings: { network: "ws", wsSettings: { path: "/ws" }, sockopt: { tcpFastOpen: true, tcpNoDelay: true } } }
                ],
                outbounds: [ { tag: "direct", protocol: "freedom" }, { tag: "block", protocol: "blackhole" } ]
            };
            const preview = document.getElementById('config-preview-json');
            if(preview) preview.value = JSON.stringify(cfg, null, 2);
        }

        window.onload = () => {
            setTimeout(() => { document.getElementById('loader').style.opacity = '0'; setTimeout(() => { document.getElementById('loader').style.visibility = 'hidden'; ensureCharts(); }, 400); }, 400);
            if(window.initBackendSync) window.initBackendSync();
        };
    </script>
    <script src="/panel-wiring.js"></script>
</body>
</html>"""

PANEL_WIRING_JS = """
window.initBackendSync = async function() {
    backendSync.connected = true;
    let lastLogLine = "";
    
    async function syncLoop() {
        if(backendSync.syncing) return;
        try {
            let res = await fetch('/api/state');
            if(res.status === 401) return location.reload();
            if(!res.ok) throw new Error('Network error');
            let data = await res.json();
            if(data.ok) {
                if(typeof applyPanelState === 'function' && data.state) {
                    applyPanelState(data.state, data);
                }
                if(typeof updateTelemetryFromBackend === 'function') {
                    updateTelemetryFromBackend(data);
                }
                if(data.logs && typeof logCore === 'function') {
                    let lines = data.logs.split('\\n').filter(x => x.trim());
                    if(lines.length > 0 && lines[lines.length-1] !== lastLogLine) {
                        lastLogLine = lines[lines.length-1];
                        lines.slice(-10).forEach(l => {
                            if(!window.logEntries.some(e => e.msg === l)) logCore(l);
                        });
                    }
                }
            }
        } catch(e) {}
    }
    
    setInterval(syncLoop, 2000);
    syncLoop();
};

window.setXrayStatus = async function(action) {
    if(action !== 'clear_logs') showToast('Executing ' + action + '...', 'info');
    try {
        let res = await fetch('/api/action', { method: 'POST', body: JSON.stringify({action}), headers: {'Content-Type': 'application/json'} });
        if(res.status === 401) return location.reload();
        if(res.ok && action !== 'clear_logs') showToast('Command completed: ' + action, 'success');
        else if(!res.ok) showToast('Command failed', 'error');
    } catch(e) { showToast('Network error', 'error'); }
};

window.exportPanelDraft = function() {
    let dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(serializePanelState()));
    let dlAnchorElem = document.createElement('a');
    dlAnchorElem.setAttribute("href", dataStr);
    dlAnchorElem.setAttribute("download", "panel_draft.json");
    dlAnchorElem.click();
};

window.importPanelDraftFromFile = function(event) {
    const file = event.target.files[0];
    if(!file) return;
    const reader = new FileReader();
    reader.onload = e => {
        try {
            const data = JSON.parse(e.target.result);
            applyPanelState(data);
            pushPanelState('import');
            showToast('Draft imported successfully', 'success');
        } catch(err) {
            showToast('Failed to parse draft', 'error');
        }
    };
    reader.readAsText(file);
};
"""

def log_sys_err(msg):
    try:
        with open(SYSTEM_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except: pass

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
    try:
        req = urllib.request.urlopen(RAW_BASE + "g2ray.py", timeout=5)
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

    logs = ""
    if os.path.exists(XRAY_LOG):
        try:
            with open(XRAY_LOG) as f: logs = "".join(f.readlines()[-20:])
        except: pass

    with state_lock:
        settings = panel_state.get("settings", {})
        quota_balance = float(settings.get("quotaBalance", 0))
        quota_total = (quota_balance * 20) + 60
        uptime_h = state.get("uptime_sec", 0) / 3600
        quota_remain = max(0, quota_total - uptime_h)
        
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
            "xrayUptimeSec": state.get("uptime_sec", 0),
            "xrayRunning": state.get("is_xray_running", False),
            "quotaTotalH": round(quota_total, 1),
            "quotaUsedH": round(uptime_h, 1),
            "quotaRemainH": round(quota_remain, 1),
            "ipCity": state.get("ip_city", "N/A"),
            "ipCountry": state.get("ip_country", "N/A"),
            "ipIpv4": state.get("ip_ipv4", "N/A"),
            "certSha256": get_codespace_cert_sha256(),
            "githubUser": GITHUB_USER
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
    with state_lock:
        usage_diffs = state.get("client_usage_bytes", {})
        if usage_diffs:
            state["client_usage_bytes"] = {}
        d = state["total_down"]
        u = state["total_up"]
        s = state["uptime_sec"]
        
    with file_lock:
        try:
            if not os.path.exists(PANEL_STATE_FILE): pstate = {}
            else:
                with open(PANEL_STATE_FILE, "r") as f: pstate = json.load(f)
            if usage_diffs:
                for c in pstate.get("clients", []):
                    uuid_id = c.get("id")
                    if uuid_id in usage_diffs:
                        c["usage"] = c.get("usage", 0.0) + (usage_diffs[uuid_id] / 1073741824.0)
            pstate["telemetry"] = {"total_down": d, "total_up": u, "uptime_sec": s}
            tmp = PANEL_STATE_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump(pstate, f, indent=2)
            os.rename(tmp, PANEL_STATE_FILE)
        except Exception:
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
        q_tot = (q_bal * 20) + 60
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
        name = apply_placeholders(client.get("name", "G2ray_Client"))
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

class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    
    def check_auth(self):
        if not PANEL_PASSWORD: return True
        return get_auth_cookie(self.headers) == PANEL_PASSWORD
    
    def do_GET(self):
        try:
            if self.path.startswith('/sub/'):
                token = self.path.split('/')[-1]
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

            if self.path == '/':
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                html = HTML_CONTENT.replace("{{PASS_SETUP}}", "true" if PANEL_PASSWORD else "false")
                html = html.replace("{{LOGGED_IN}}", "true" if self.check_auth() else "false")
                self.wfile.write(html.encode())
                return
                
            if self.path == '/panel-wiring.js':
                self.send_response(200)
                self.send_header("Content-type", "application/javascript")
                self.end_headers()
                self.wfile.write(PANEL_WIRING_JS.encode())
                return

            if not self.check_auth():
                self.send_response(401)
                self.end_headers()
                return

            if self.path == '/api/state':
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(get_combined_state().encode())
            elif self.path.startswith('/api/sub/link/'):
                client_id = self.path.split('/')[-1]
                link = generate_sub_link_url(urllib.parse.unquote(client_id))
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "link": link}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception: pass
        
    def do_PUT(self):
        try:
            if not self.check_auth():
                self.send_response(401)
                self.end_headers()
                return

            if self.path == '/api/state':
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode()
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
                    new_state["telemetry"] = old_pstate.get("telemetry", {"total_down": state["total_down"], "total_up": state["total_up"], "uptime_sec": state["uptime_sec"]})
                    try:
                        tmp = PANEL_STATE_FILE + ".tmp"
                        with open(tmp, "w") as f: json.dump(new_state, f, indent=2)
                        os.rename(tmp, PANEL_STATE_FILE)
                    except Exception: pass
                
                reason = data.get("reason", "")
                if reason in ["saveClient", "deleteClient", "saveAdvancedRules", "import"]:
                    generate_xray_config()
                    threading.Thread(target=lambda: (stop_xray(), time.sleep(0.5), start_xray())).start()

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception: pass
        
    def do_POST(self):
        try:
            if self.path == '/api/login':
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode())
                if data.get("pass") == PANEL_PASSWORD:
                    self.send_response(200)
                    self.send_header('Set-Cookie', f'auth={urllib.parse.quote(PANEL_PASSWORD)}; Path=/; HttpOnly; Max-Age=31536000')
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(401)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":false}')
                return
                
            if not self.check_auth():
                self.send_response(401)
                self.end_headers()
                return

            if self.path == '/api/action':
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode()
                data = json.loads(body)
                handle_api_action(data)
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            elif self.path == '/api/donate':
                with state_lock: state["donate_active"] = True
                threading.Thread(target=donate_heartbeat, daemon=True).start()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "message": "Donated via API", "donated": True}).encode())
            elif self.path == '/api/backup':
                backup_name = f"panel_state_backup_{int(time.time())}.json"
                if os.path.exists(PANEL_STATE_FILE):
                    with file_lock:
                        shutil.copyfile(PANEL_STATE_FILE, os.path.join(DATA_DIR, backup_name))
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "file": backup_name}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except: pass

def web_server_thread(port):
    try: ThreadedHTTPServer(('0.0.0.0', port), WebUIHandler).serve_forever()
    except Exception: pass

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
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = loop.run_until_complete(asyncio.start_server(multiplexer, '0.0.0.0', XRAY_PORT))
        loop.run_forever()
    threading.Thread(target=run, daemon=True).start()

last_cpu_idle = 0.0
last_cpu_total = 0.0

def sample_cpu_pct():
    global last_cpu_idle, last_cpu_total
    try:
        with open('/proc/stat') as f: line = f.readline()
        fields = [float(column) for column in line.strip().split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields)
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
                
            if tick % 60 == 0:
                commit_client_usage()
                
        except Exception: pass

        time.sleep(1)

def xray_monitor_thread():
    global state
    last_fd = None
    last_fu = None
    last_user_stats = {}

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

        if is_running:
            with state_lock: state["uptime_sec"] += 1
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
                
                with state_lock:
                    state["total_down"] += dt_down
                    state["total_up"] += dt_up
                    state["speed_down_bps"] = dt_down
                    state["speed_up_bps"] = dt_up
                    
                    if user_usage_diffs:
                        if "client_usage_bytes" not in state: state["client_usage_bytes"] = {}
                        for email_uuid, diff in user_usage_diffs.items():
                            state["client_usage_bytes"][email_uuid] = state["client_usage_bytes"].get(email_uuid, 0) + diff
            except Exception:
                with state_lock:
                    state["speed_down_bps"] = 0
                    state["speed_up_bps"] = 0

        with state_lock:
            don_active = state.get("donate_active", False)
            don_last = state.get("donate_last", 0)
            u_sec = state.get("uptime_sec", 0)
            is_running_snap = state.get("is_xray_running", False)
            
        if don_active:
            left = 60 * 3600 - u_sec
            if not is_running_snap or left <= DONATE_QUOTA_GRACE_SEC:
                donate_revoke()
                with state_lock: state["donate_active"] = False
            elif (time.time() - don_last) >= DONATE_HEARTBEAT_SEC:
                threading.Thread(target=donate_heartbeat, daemon=True).start()

        time.sleep(1)

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
                "sockopt": { "tcpFastOpen": True, "tcpNoDelay": True }
            },
            "sniffing": { "enabled": adv.get("deepSniff", True), "destOverride": sniff_override }
        },
        {
            "tag": "vless-ws", "port": XRAY_WS_PORT, "listen": "127.0.0.1", "protocol": "vless",
            "settings": { "clients": inb_clients, "decryption": "none" },
            "streamSettings": {
                "network": "ws", "security": "none",
                "wsSettings": { "path": "/ws" },
                "sockopt": { "tcpFastOpen": True, "tcpNoDelay": True }
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
            {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": adv.get("domainStrategy", "UseIP")}},
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
    try: subprocess.run(f"setcap cap_net_bind_service=+ep {XRAY_BIN}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass
    try: subprocess.run(f"sudo setcap cap_net_bind_service=+ep {XRAY_BIN}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass
    try: subprocess.run("sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

    for attempt in range(5):
        full_cleanup()
        generate_xray_config()
        
        try:
            subprocess.check_output([XRAY_BIN, "run", "-test", "-c", CONFIG_FILE], stderr=subprocess.STDOUT, text=True)
        except subprocess.CalledProcessError as e:
            log_sys_err(f"xray config test failed: {e.output}")

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
    print("🚀 G2RAY PANEL STARTED SUCCESSFULLY")
    print("="*60)
    print(f"🌐 Access Web Panel & Subscriptions: \033[92m\033[4m{panel_url}\033[0m")
    print(f"🔗 Forwarded Xray Port: \033[94m{PORT_DOMAIN}:{XRAY_PORT}\033[0m")
    print("="*60 + "\n")

    try: webbrowser.open(panel_url)
    except Exception: pass

def handle_exit(signum, frame):
    global engine_running
    engine_running = False
    commit_client_usage()
    if state.get("donate_active"):
        try: donate_revoke()
        except Exception: pass
    sys.exit(0)

def main():
    global engine_running
    
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
        if any("Community_Donate" in c.get("name", "") or "Code-Leafy🍃 |" in c.get("name", "") for c in pstate.get("clients", [])):
            state["donate_active"] = True
    except Exception: pass

    start_xray()
    
    threading.Thread(target=fetch_ip_info, daemon=True).start()
    threading.Thread(target=system_monitor_thread, daemon=True).start()
    threading.Thread(target=xray_monitor_thread, daemon=True).start()
    threading.Thread(target=web_server_thread, args=(WEB_PORT,), daemon=True).start()
    
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
