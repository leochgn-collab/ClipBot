#!/usr/bin/env python3
"""Interface web de ClipBot.

    python web.py --set-password      # définit le mot de passe (une fois)
    python web.py                     # lance l'interface (port 8080 par défaut)
"""

import argparse
import base64
import fcntl
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import struct
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

import yaml
from flask import (Flask, abort, flash, redirect, render_template_string, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.yaml"
SECRETS = BASE / "secrets"
PASS_FILE = SECRETS / "web_password"
KEY_FILE = SECRETS / "web_secret_key"
TOTP_FILE = SECRETS / "web_totp"
JOB_LOG = BASE / "logs" / "dernier_lancement.log"

app = Flask(__name__)
WEB = {}  # section « web » de la config, lue au démarrage


# --------------------------------------------------------------------------
# Sécurité : IP réelle, liste blanche, double authentification, en-têtes
# --------------------------------------------------------------------------
def _nets(values):
    out = []
    for v in values or []:
        try:
            out.append(ipaddress.ip_network(str(v), strict=False))
        except ValueError:
            print(f"⚠️ IP/réseau invalide ignoré dans la config : {v}")
    return out


def _in(ip, nets):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def client_ip():
    """IP du visiteur. X-Forwarded-For n'est cru que s'il vient de Zoraxy (proxy de confiance) :
    on prend alors la dernière IP de la liste, celle ajoutée par Zoraxy, impossible à falsifier."""
    remote = request.remote_addr or ""
    if _in(remote, WEB.get("_proxies", [])):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff.strip():
            return xff.split(",")[-1].strip()
    return remote


def via_https():
    return (_in(request.remote_addr or "", WEB.get("_proxies", []))
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https")


def totp_now(secret_b32, at=None):
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((at or time.time()) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_ok(code):
    if not TOTP_FILE.exists():
        return True
    secret = TOTP_FILE.read_text().strip()
    code = (code or "").replace(" ", "")
    now = time.time()
    return any(hmac.compare_digest(totp_now(secret, now + d * 30), code) for d in (-1, 0, 1))


@app.before_request
def ip_filter():
    allowed = WEB.get("_allowed")
    if allowed and not _in(client_ip(), allowed):
        abort(403)


@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self'; media-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    resp.headers["Cache-Control"] = "no-store"
    if via_https():
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


class SecureCookies:
    """Ajoute « Secure » aux cookies quand la page passe par Zoraxy en HTTPS
    (le cookie de session est écrit après les hooks Flask, d'où ce filtre WSGI)."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        https = (_in(environ.get("REMOTE_ADDR", ""), WEB.get("_proxies", []))
                 and environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https")

        def sr(status, headers, exc_info=None):
            if https:
                headers = [(k, v + "; Secure") if k.lower() == "set-cookie" and "Secure" not in v
                           else (k, v) for k, v in headers]
            return start_response(status, headers, exc_info)
        return self.wsgi(environ, sr)


app.wsgi_app = SecureCookies(app.wsgi_app)


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def secret_key():
    SECRETS.mkdir(exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_hex(32))
        KEY_FILE.chmod(0o600)
    return KEY_FILE.read_text().strip()


def load_cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def save_cfg(cfg):
    shutil.copy(CONFIG, CONFIG.with_suffix(".yaml.bak"))
    CONFIG.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    CONFIG.chmod(0o600)


def resolve(p):
    p = Path(p)
    return p if p.is_absolute() else BASE / p


def db():
    path = resolve(load_cfg().get("state_db", "clipbot.db"))
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS clips (
        source_id TEXT PRIMARY KEY, platform TEXT, channel TEXT, title TEXT, status TEXT,
        youtube_id TEXT, tiktok_id TEXT, tries INTEGER DEFAULT 0, error TEXT, updated TEXT)""")
    return con


def job_running():
    try:
        with open(BASE / "clipbot.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True


def start_job(mode):
    """Dépose une demande ; l'unité systemd clipbot-run.path lance ClipBot
    (même environnement que les passages automatiques)."""
    JOB_LOG.parent.mkdir(exist_ok=True)
    JOB_LOG.write_text(f"[{datetime.now():%d/%m %H:%M:%S}] demande : {mode} (démarrage…)\n")
    trig = BASE / "trigger"
    trig.mkdir(exist_ok=True)
    tmp = trig / ".request"
    tmp.write_text(mode)
    tmp.rename(trig / "request")


def auto_rediffs(cfg):
    try:
        f = resolve(cfg.get("work_dir", "work")) / "rediffs_auto.json"
        return json.loads(f.read_text()) if f.exists() else {}
    except Exception:
        return {}


def model_info(cfg):
    try:
        import clipbot
        f = clipbot.model_file(cfg)
        if not f.exists():
            return "Apprentissage : pas encore d'exemples (au volume sonore pour l'instant)."
        m = json.loads(f.read_text())
        n, need = m.get("n_pos", 0), clipbot.MODEL_MIN_EXAMPLES
        if n < need:
            return f"Apprentissage en cours : {n}/{need} passages clippés vus (au volume sonore pour l'instant)."
        return f"Modèle appris actif : {n} passages clippés sur {m.get('n_lives', 0)} live(s), mis à jour le {m.get('date', '')[:10]}."
    except Exception:
        return "Apprentissage : état illisible."


def tail(path, n=200):
    """Les n dernières lignes, en ne lisant que la fin du fichier (rapide même si le log est gros)."""
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 200 * n))
        data = fh.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-n:])


_timer_cache = {"t": 0, "v": None}


def timer_info():
    if _timer_cache["v"] is not None and time.time() - _timer_cache["t"] < 30:
        return _timer_cache["v"]
    _timer_cache["v"] = _timer_info()
    _timer_cache["t"] = time.time()
    return _timer_cache["v"]


def _timer_info():
    try:
        nxt = subprocess.run(["systemctl", "show", "clipbot.timer", "-p",
                              "NextElapseUSecRealtime", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        active = subprocess.run(["systemctl", "is-active", "clipbot.timer"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        return active, nxt or "—"
    except Exception:
        return "inconnu", "—"


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def check_csrf():
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "") or "-"):
        abort(400)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            check_csrf()
        return f(*a, **kw)
    return wrapper


def safe_back(default="history"):
    """Revient à la page précédente seulement si elle est sur ce site."""
    ref = request.referrer or ""
    host = request.host_url
    if ref.startswith(host):
        return redirect("/" + ref[len(host):])
    return redirect(url_for(default))


app.jinja_env.globals["csrf_token"] = csrf_token


# --------------------------------------------------------------------------
# Gabarit
# --------------------------------------------------------------------------
LAYOUT = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="ClipBot">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/app-icon.png">
<title>ClipBot</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262a33;--txt:#e6e8ec;--mut:#8b93a1;--acc:#6d8cff;--ok:#3fb950;--ko:#f85149;--warn:#d29922}
a{color:var(--acc)}.row form{flex:none}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--txt)}
nav{display:flex;gap:4px;flex-wrap:wrap;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card)}
nav b{margin-right:12px}nav a{color:var(--mut);text-decoration:none;padding:6px 10px;border-radius:6px}
nav a.on,nav a:hover{color:var(--txt);background:var(--line)}nav .r{margin-left:auto}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
h1{font-size:22px;margin:0 0 16px}h2{font-size:16px;margin:24px 0 10px;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.stat .n{font-size:28px;font-weight:600}.stat .l{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500}
.b{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px}
.b.done{background:#1b3a24;color:var(--ok)}.b.error{background:#3d1d1d;color:var(--ko)}.b.new{background:var(--line);color:var(--mut)}
button,.btn{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.sec{background:var(--line)}button.ko{background:#6e2626}button.sm{padding:3px 9px;font-size:13px}
input,textarea,select{width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:8px;font:inherit}
textarea{font-family:ui-monospace,monospace;font-size:13px}label{display:block;color:var(--mut);font-size:13px;margin:10px 0 4px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.row>*{flex:1}
pre{background:#0a0c10;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;max-height:520px;font-size:12.5px;white-space:pre-wrap}
.flash{padding:10px 14px;border-radius:8px;background:#1d2a4a;margin-bottom:14px}
.mut{color:var(--mut)}.err{color:var(--ko);font-size:13px}
.media{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.media img,.media video{width:100%;border-radius:8px;border:1px solid var(--line);background:#000}
.tabbar{display:none}
@media (max-width:760px){
 body{font-size:16px;padding-bottom:calc(78px + env(safe-area-inset-bottom))}
 nav{display:none}
 main{padding:calc(14px + env(safe-area-inset-top)) 14px 20px}
 h1{font-size:24px}
 .card{padding:14px;border-radius:14px}
 .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
 .stat .n{font-size:24px}
 .row{flex-direction:column;align-items:stretch}
 .row>*{width:100%}
 .row form{flex:none;width:100%}
 button,.btn{min-height:46px;font-size:16px;width:100%;text-align:center}
 button.sm{min-height:40px;font-size:15px}
 input,select,textarea{font-size:16px;min-height:44px}
 input[type=checkbox]{min-height:auto;width:22px;height:22px;vertical-align:middle}
 table,tbody,tr,td{display:block;width:100%}
 table tr:first-child{display:none}
 table tr{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:10px}
 td{border:0;padding:4px 0}
 td form.row{flex-direction:row;flex-wrap:wrap;gap:8px}
 td form.row button{flex:1 1 40%}
 .media{grid-template-columns:repeat(2,minmax(0,1fr))}
 pre{font-size:11.5px;max-height:60vh}
 .tabbar{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:20;
  background:rgba(24,27,34,.96);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
 .tabbar a{display:flex;flex-direction:column;align-items:center;gap:3px;color:var(--mut);text-decoration:none;
  font-size:11px;padding:6px 0;min-height:44px;border-radius:10px}
 .tabbar a.on{color:var(--acc)}
 .tabbar svg{width:24px;height:24px}
}
</style></head><body>
{% if session.ok %}<nav><b>🎬 ClipBot</b>
{% for p,l in [('home','Tableau de bord'),('available',"File d'attente"),('history','Historique'),('stats','Statistiques'),('channels','Chaînes'),('settings','Réglages'),('cookies','Cookies'),('media','Captures'),('logs','Logs')] %}
<a href="{{url_for(p)}}" class="{{'on' if request.endpoint==p}}">{{l}}</a>{% endfor %}
<form class="r" method="post" action="{{url_for('logout')}}" style="margin-left:auto"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="sec sm">Déconnexion</button></form></nav>{% endif %}
<main>{% for m in get_flashed_messages() %}<div class="flash">{{m}}</div>{% endfor %}
{{ body|safe }}</main>
{% if session.ok %}<nav class="tabbar" aria-label="Menu">
{% set more = request.endpoint in ('menu','stats','trends','channels','cookies','media','logs','tiktok_login') %}
<a href="{{url_for('home')}}" class="{{'on' if request.endpoint=='home'}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>Accueil</a>
<a href="{{url_for('available')}}" class="{{'on' if request.endpoint=='available'}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/></svg>Clips</a>
<a href="{{url_for('history')}}" class="{{'on' if request.endpoint=='history'}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>Historique</a>
<a href="{{url_for('settings')}}" class="{{'on' if request.endpoint=='settings'}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>Réglages</a>
<a href="{{url_for('menu')}}" class="{{'on' if more}}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>Plus</a>
</nav>{% endif %}
<div id="ld" style="position:fixed;top:0;left:0;height:3px;width:0;background:var(--acc);z-index:99;transition:width 8s cubic-bezier(.1,.7,.1,1)"></div>
<script>
(function(){
 const pre=new Set();
 function prefetch(a){ if(!a||!a.href||a.target||a.href.indexOf(location.origin)!==0||pre.has(a.href)) return;
   pre.add(a.href); const l=document.createElement("link"); l.rel="prefetch"; l.href=a.href; document.head.appendChild(l); }
 document.addEventListener("touchstart",e=>prefetch(e.target.closest("a")),{passive:true});
 document.addEventListener("mouseover",e=>prefetch(e.target.closest("a")),{passive:true});
 function go(){ const b=document.getElementById("ld"); b.style.width="85%"; }
 document.addEventListener("click",e=>{const a=e.target.closest("a"); if(a&&a.href&&!a.target&&a.href.indexOf(location.origin)===0&&!e.metaKey) go();});
 document.addEventListener("submit",e=>{ go(); const b=e.target.querySelector("button"); if(b){ b.disabled=true; b.style.opacity=.6; } });
 window.addEventListener("pageshow",()=>{document.getElementById("ld").style.width="0"});
})();
</script></body></html>"""


def page(tpl, **ctx):
    body = render_template_string(tpl, **ctx)
    return render_template_string(LAYOUT, body=body)


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------
_fails = {}          # ip -> [nb d'échecs, heure du dernier]
_global_fails = []   # horodatages de tous les échecs (protection contre les IP multiples)


def _blocked(ip):
    now = time.time()
    for k in [k for k, (_, t) in _fails.items() if now - t > 900]:
        del _fails[k]
    _global_fails[:] = [t for t in _global_fails if now - t < 600]
    n, _ = _fails.get(ip, (0, 0))
    return n >= 5 or len(_global_fails) >= 20


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        check_csrf()
        ip = client_ip()
        if _blocked(ip):
            flash("Trop d'essais, réessaie dans 10 minutes.")
            return redirect(url_for("login"))
        good_pw = PASS_FILE.exists() and check_password_hash(
            PASS_FILE.read_text().strip(), request.form.get("password", ""))
        if good_pw and totp_ok(request.form.get("code")):
            session.clear()
            session["ok"] = True
            session.permanent = True
            _fails.pop(ip, None)
            nxt = request.args.get("next", "/")
            ok_next = nxt.startswith("/") and not nxt.startswith(("//", "/\\")) and "\\" not in nxt
            return redirect(nxt if ok_next else "/")
        n, _ = _fails.get(ip, (0, 0))
        _fails[ip] = [n + 1, time.time()]
        _global_fails.append(time.time())
        time.sleep(1)
        flash("Identifiants incorrects.")
    return page("""<div class="card" style="max-width:360px;margin:60px auto">
<h1>🎬 ClipBot</h1><form method="post"><input type="hidden" name="csrf" value="{{csrf_token()}}">
<label>Mot de passe</label><input type="password" name="password" autocomplete="current-password" autofocus>
{% if totp %}<label>Code de double authentification</label>
<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="123456">{% endif %}
<br><br><button>Se connecter</button></form></div>""", totp=TOTP_FILE.exists())


@app.route("/menu")
@login_required
def menu():
    return page("""<h1>Plus</h1>
<div class="card" style="display:flex;flex-direction:column;gap:10px">
<a class="btn sec" href="{{url_for('stats')}}">📈 Statistiques (ce qui marche)</a>
<a class="btn sec" href="{{url_for('trends')}}">🔥 Tendances (ce qui marche chez les clippers)</a>
<a class="btn sec" href="{{url_for('channels')}}">Chaînes autorisées</a>
<a class="btn sec" href="{{url_for('tiktok_login')}}">📱 Connexion TikTok (QR code)</a>
<a class="btn sec" href="{{url_for('cookies')}}">Cookies</a>
<a class="btn sec" href="{{url_for('media')}}">Captures et aperçus</a>
<a class="btn sec" href="{{url_for('logs')}}">Logs</a>
<a class="btn sec" href="{{cfg.youtube_url or 'https://www.youtube.com/@redifstream2'}}" target="_blank" rel="noopener">Voir sur YouTube</a>
{% if cfg.tiktok_url %}<a class="btn sec" href="{{cfg.tiktok_url}}" target="_blank" rel="noopener">Voir sur TikTok</a>{% endif %}
<form method="post" action="{{url_for('logout')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="ko">Déconnexion</button></form>
</div>
<p class="mut" style="font-size:13px">Astuce iPhone : dans Safari, Partager → « Sur l'écran d'accueil » pour ouvrir ClipBot comme une appli.</p>""", cfg=load_cfg())


@app.route("/manifest.webmanifest")
def manifest():
    return app.response_class(json.dumps({
        "name": "ClipBot", "short_name": "ClipBot", "start_url": "/", "display": "standalone",
        "background_color": "#0f1115", "theme_color": "#0f1115",
        "icons": [{"src": "/app-icon.png", "sizes": "512x512", "type": "image/png"}]}),
        mimetype="application/manifest+json")


@app.route("/app-icon.png")
def app_icon():
    """Icône de l'appli (ton logo, s'il a été envoyé). Public : nécessaire à l'écran d'accueil."""
    p = logo_path()
    if not p.exists():
        abort(404)
    return send_from_directory(p.parent, p.name, max_age=86400)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Tableau de bord
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    con = db()
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    since = datetime.utcfromtimestamp(time.time() - 86400).isoformat()
    stats = {
        "24h": q("SELECT COUNT(*) FROM clips WHERE status='done' AND updated>=?", since),
        "total": q("SELECT COUNT(*) FROM clips WHERE status='done'"),
        "err": q("SELECT COUNT(*) FROM clips WHERE status='error'"),
    }
    last = con.execute("SELECT * FROM clips ORDER BY updated DESC LIMIT 8").fetchall()
    cfg = load_cfg()
    active, nxt = timer_info()
    sch = cfg.get("schedule") or {}
    last_done = con.execute("SELECT MAX(updated) FROM clips WHERE status='done'").fetchone()[0]
    if last_done and sch.get("interval_minutes"):
        from datetime import timedelta, timezone
        t = datetime.fromisoformat(last_done)
        t = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) + timedelta(minutes=int(sch["interval_minutes"]))
        from zoneinfo import ZoneInfo
        nxt = "vers " + t.astimezone(ZoneInfo("Europe/Paris")).strftime("%H:%M") if t > datetime.now(timezone.utc) else "au prochain passage (≤ 15 min)"
    return page("""<h1>Tableau de bord</h1>
{% if ytblock %}<div class="card err" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
<div style="flex:1 1 220px">⏸ <b>YouTube en pause jusqu'à {{ytblock}}</b> : YouTube a demandé de confirmer que ce n'est pas un robot.
ClipBot continue avec Twitch ; les rediffs YouTube attendent. Si ça revient souvent : Cookies → Téléchargements YouTube.</div>
<form method="post" action="{{url_for('youtube_unblock')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="sm sec">Réessayer YouTube maintenant</button></form></div>{% endif %}
<div class="grid">
<div class="card stat"><div class="n">{{s['24h']}} / {{cfg.get('max_posts_per_day',6)}}</div><div class="l">publiés sur 24 h</div></div>
<div class="card stat"><div class="n">{{s.total}}</div><div class="l">publiés au total</div></div>
<div class="card stat"><div class="n" style="color:{{'var(--ko)' if s.err else 'inherit'}}">{{s.err}}</div><div class="l">en erreur</div></div>
<div class="card stat"><div class="n" style="font-size:16px;margin-top:8px">{{'⏸ en pause' if cfg.paused else ('🟢 actif' if active=='active' else '⚪ ' ~ active)}}</div><div class="l">automatique · prochain clip : {{nxt}} · actif {{(cfg.schedule or {}).start_hour or 0}}h-{{(cfg.schedule or {}).end_hour or 24}}h</div></div>
</div>
<div class="card"><div class="row" style="flex:none">
{% if running %}<div style="flex:1 1 100%">
<div><b id="st-step">⏳ {{ st.step if st else 'Un passage est en cours…' }}</b> <span class="mut" id="st-time"></span></div>
<div class="mut" id="st-detail" style="font-size:13px">{{ st.detail if st else '' }}</div></div>
<form method="post" action="{{url_for('stop')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="ko">⏹ Arrêter</button></form>
<a class="btn sec" href="{{url_for('logs')}}">Voir le suivi</a>
<script>
const t0={{ (st.started if st else 0)|tojson }};
function tick(){fetch("{{url_for('status')}}",{credentials:"same-origin"}).then(r=>r.json()).then(d=>{
 if(!d.running){location.reload();return}
 if(d.step){document.getElementById("st-step").textContent="⏳ "+d.step;document.getElementById("st-detail").textContent=d.detail||""}
 const s=Math.round(Date.now()/1000-(d.started||t0)); if(s>0&&s<86400) document.getElementById("st-time").textContent="· depuis "+(s>=60?Math.floor(s/60)+" min ":"")+(s%60)+" s";
}).catch(()=>{})}
tick();setInterval(tick,3000);
</script>
{% else %}
<form method="post" action="{{url_for('run')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="mode" value="normal"><button>▶ Lancer maintenant</button></form>
<form method="post" action="{{url_for('run')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="mode" value="dry"><button class="sec">🧪 Essai sans publier</button></form>
<form method="post" action="{{url_for('run')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="mode" value="check"><button class="sec">🔑 Vérifier les connexions</button></form>
{% endif %}
<form method="post" action="{{url_for('pause')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="{{'' if cfg.paused else 'sec'}}">{{ '▶ Reprendre' if cfg.paused else '⏸ Pause' }}</button></form>
<a class="btn sec" href="{{cfg.youtube_url or 'https://www.youtube.com/@redifstream2'}}" target="_blank" rel="noopener">▶ Voir sur YouTube</a>
{% if cfg.tiktok_url %}<a class="btn sec" href="{{cfg.tiktok_url}}" target="_blank" rel="noopener">♪ Voir sur TikTok</a>{% endif %}
</div>{% if cfg.paused %}<p class="err" style="margin:10px 0 0">⏸ En pause : aucune publication automatique. Les essais et « Publier maintenant » marchent toujours.</p>{% endif %}</div>
<h2>Dernière activité</h2><div class="card">{% include_body %}</div>""".replace(
        "{% include_body %}", HISTORY_TABLE), s=stats, cfg=cfg, rows=last,
        running=job_running(), active=active, nxt=nxt, st=read_status(), ytblock=yt_block_info())


HISTORY_TABLE = """{% if rows %}<table><tr><th>Date</th><th>Chaîne</th><th>Titre</th><th>Statut</th><th>Liens</th><th></th></tr>
{% for r in rows %}<tr>
<td class="mut">{{(r.updated or '')[:16].replace('T',' ')}}</td>
<td>{{r.channel}}<div class="mut" style="font-size:12px">{{ 'live (moment fort)' if r.platform == 'twitch-vod' else r.platform }}</div></td>
<td>{{r.title}}{% if r.error %}<div class="err">{{r.error}}</div>{% endif %}</td>
<td><span class="b {{r.status or 'new'}}">{{ {'done':'publié','error':'erreur'}.get(r.status, 'en cours') }}</span>{% if r.tries %}<div class="mut" style="font-size:12px">{{r.tries}} essai(s)</div>{% endif %}</td>
<td>{% if r.youtube_id and r.youtube_id!='navigateur' %}<a href="https://youtube.com/shorts/{{r.youtube_id}}" target="_blank">YouTube</a>{% elif r.youtube_id %}YouTube ✓{% endif %}
{% if r.tiktok_id %}<br>TikTok ✓{% endif %}</td>
<td><form method="post" action="{{url_for('clip_action')}}" class="row" style="flex:none;gap:4px">
<input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="sid" value="{{r.source_id}}">
{% if r.status=='error' %}<button class="sm" name="a" value="retry">Réessayer</button>
<button class="sm" formaction="{{url_for('run')}}" name="mode" value="one">Publier maintenant</button>{% endif %}
<button class="sm sec" name="a" value="forget" title="Oublier : le clip pourra être reposté">Oublier</button></form></td>
</tr>{% endfor %}</table>{% else %}<p class="mut">Aucun clip pour l'instant.</p>{% endif %}"""


SID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")


@app.route("/run", methods=["POST"])
@login_required
def run():
    if job_running() or (BASE / "trigger" / "request").exists():
        flash("Un passage est déjà en cours, réessaie dans quelques minutes.")
        return redirect(url_for("logs"))
    mode = request.form.get("mode")
    if mode == "one":
        sid = request.form.get("sid", "")
        if not SID_RE.match(sid):
            abort(400)
        start_job("one:" + sid)
        flash("Publication de ce clip lancée ! Suis l'avancement ici.")
    else:
        start_job(mode if mode in ("dry", "check") else "normal")
        flash("Lancé ! Suis l'avancement dans « Logs ».")
    return redirect(url_for("logs"))


def _qr_files():
    w = resolve(load_cfg().get("work_dir", "work"))
    return w / "tiktok_qr.png", w / "tiktok_login.json"


@app.route("/tiktok-login", methods=["GET", "POST"])
@login_required
def tiktok_login():
    png, st = _qr_files()
    if request.method == "POST":
        if job_running() or (BASE / "trigger" / "request").exists():
            flash("Un passage est en cours : attends qu'il finisse (ou Arrêter), puis réessaie.")
        else:
            png.unlink(missing_ok=True)
            st.parent.mkdir(parents=True, exist_ok=True)
            st.write_text(json.dumps({"state": "wait", "msg": "Démarrage du navigateur…", "t": time.time()}))
            start_job("tiktokqr")
        return redirect(url_for("tiktok_login"))
    return page("""<h1>Connexion TikTok</h1>
<div class="card">
<p>1) Touche <b>Afficher le QR code</b>. 2) Fais une <b>capture d'écran</b> du QR code.
3) Dans l'app TikTok : <b>Profil → ☰ → icône de scan</b> (en haut), puis l'icône <b>photo</b> pour choisir ta capture
(ou scanne-le avec un autre appareil). 4) Valide dans TikTok : ClipBot est connecté.</p>
<p class="mut" style="font-size:13px">Connecte le compte TikTok de RedifStream, pas ton compte perso. Le QR code change toutes les ~2 min : refais une capture s'il a changé.</p>
<form method="post"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button>📱 Afficher le QR code</button></form>
<p id="qr-msg" style="margin-top:14px"><b></b></p>
<img id="qr-img" alt="QR code TikTok" style="display:none;width:260px;max-width:90%;background:#fff;padding:12px;border-radius:12px">
</div>
<script>
let lastT=0;
function poll(){fetch("{{url_for('tiktok_login_status')}}",{credentials:"same-origin"}).then(r=>r.json()).then(d=>{
 const m=document.querySelector("#qr-msg b"); m.textContent=d.msg||"";
 const img=document.getElementById("qr-img");
 if(d.state==="qr"&&d.png_t&&d.png_t!==lastT){lastT=d.png_t;img.src="{{url_for('tiktok_qr_png')}}?t="+d.png_t;img.style.display="block"}
 if(d.state==="ok"||d.state==="expired"||d.state==="error"){img.style.display="none"}
}).catch(()=>{})}
poll();setInterval(poll,2500);
</script>""")


@app.route("/tiktok-login/status")
@login_required
def tiktok_login_status():
    png, st = _qr_files()
    try:
        d = json.loads(st.read_text())
    except (OSError, ValueError):
        d = {"state": "none", "msg": ""}
    d["png_t"] = int(png.stat().st_mtime * 1000) if png.exists() else 0
    return app.response_class(json.dumps(d), mimetype="application/json")


@app.route("/tiktok-login/qr.png")
@login_required
def tiktok_qr_png():
    png, _ = _qr_files()
    if not png.exists():
        abort(404)
    resp = app.response_class(png.read_bytes(), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def yt_block_info():
    try:
        f = resolve(load_cfg().get("work_dir", "work")) / "youtube_bloque"
        t = f.stat().st_mtime
        return datetime.fromtimestamp(t).strftime("%H:%M") if t > time.time() else None
    except OSError:
        return None


def read_status():
    try:
        return json.loads((BASE / "work" / "status.json").read_text())
    except (OSError, ValueError):
        return None


@app.route("/status")
@login_required
def status():
    st = read_status() or {}
    return app.response_class(json.dumps({"running": job_running(), "step": st.get("step"),
                                          "detail": st.get("detail"), "started": st.get("started")}),
                              mimetype="application/json")


@app.route("/stop", methods=["POST"])
@login_required
def stop():
    """Arrête le passage en cours (et ses téléchargements, montage, navigateur)."""
    import signal
    if not job_running():
        flash("Aucun passage en cours.")
        return redirect(url_for("home"))
    try:
        pid = int((BASE / "clipbot.lock").read_text().strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid <= 1:
        flash("Impossible de trouver le passage en cours (redémarre-le après la mise à jour).")
        return redirect(url_for("home"))
    try:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            os.kill(pid, signal.SIGTERM)
        flash("⏹ Arrêt demandé : le passage s'arrête dans quelques secondes. Tu peux relancer ensuite.")
    except (ProcessLookupError, PermissionError) as e:
        flash(f"Arrêt impossible : {e}")
    return redirect(url_for("home"))


@app.route("/stats")
@login_required
def stats():
    cfg = load_cfg()
    try:
        data = json.loads((resolve(cfg.get("work_dir", "work")) / "performances.json").read_text())
    except (OSError, ValueError):
        data = {}
    return page("""<h1>Statistiques</h1>
<p class="mut">ClipBot relève les vues et les likes de tes Shorts (toutes les 6 h, à chaque passage) et en déduit
ce qui marche. Les Shorts de moins de 24 h ne comptent pas encore. Ce qui marche le mieux passe en priorité.</p>
{% if not d.n %}<div class="card"><p class="mut">Pas encore assez de données : il faut des Shorts publiés depuis plus de 24 h.
Elles arrivent au prochain passage.</p></div>{% else %}
<p class="mut" style="font-size:13px">{{d.n}} Short(s) analysé(s) · mis à jour {{ upd }}.
« Effet » : au-dessus de 0 = fait mieux que tes Shorts habituels.</p>
{% for title, rows in d.tables.items() %}<h2>{{title}}</h2><div class="card"><table>
<tr><th>{{title}}</th><th>Shorts</th><th>Vues moy.</th><th>Likes moy.</th><th>Effet</th></tr>
{% for r in rows %}<tr><td>{{r.name}}</td><td>{{r.n}}</td><td>{{r.views}}</td><td>{{r.likes}}</td>
<td style="color:{{'var(--ok)' if r.effect > 0.05 else ('var(--ko)' if r.effect < -0.05 else 'inherit')}}">{{ '%+.2f'|format(r.effect) }}</td></tr>{% endfor %}
</table></div>{% endfor %}
<h2>Tes meilleurs Shorts</h2><div class="card"><table><tr><th>Titre</th><th>Streamer</th><th>Vues</th><th>Likes</th></tr>
{% for t in d.top %}<tr><td><a href="https://youtube.com/shorts/{{t.id}}" target="_blank" rel="noopener">{{t.title}}</a></td><td>{{t.streamer}}</td><td>{{t.views}}</td><td>{{t.likes}}</td></tr>{% endfor %}
</table></div>{% endif %}""", d=data, upd=datetime.fromtimestamp(data.get("t", 0)).strftime("%d/%m %H:%M") if data.get("t") else "")


@app.route("/ntfy-test", methods=["POST"])
@login_required
def ntfy_test():
    import clipbot
    cfg = load_cfg()
    n = cfg.get("ntfy") or {}
    if not n.get("url") or not n.get("topic"):
        flash("Remplis d'abord l'URL et le topic ntfy, puis Enregistrer.")
    else:
        clipbot.notify(cfg, "🔔 ClipBot : test", "Les notifications ClipBot arrivent bien ici.", 3, ["bell"])
        flash(f"Notification de test envoyée sur « {n['topic']} ».")
    return redirect(url_for("settings"))


@app.route("/tendances")
@login_required
def trends():
    cfg = load_cfg()
    try:
        d = json.loads((resolve(cfg.get("work_dir", "work")) / "tendances.json").read_text())
    except (OSError, ValueError):
        d = {}
    return page("""<h1>Tendances</h1>
<p class="mut">Ce qui marche chez les clippers que tu suis (Réglages → Comptes de clippers, YouTube et TikTok).
Chaque vidéo est comparée à la moyenne de <b>son</b> clipper et corrigée de son âge.
ClipBot s'en sert tout seul : les passages les plus viraux passent en premier{% if d.best_len %}, et les moments forts sont coupés à <b>{{d.best_len}} s</b> maximum (la durée qui marche le mieux){% endif %}.</p>
{% if (d.n or 0) < 5 %}<div class="card"><p class="mut">Pas encore assez de vidéos de clippers analysées ({{d.n or 0}}). Ajoute des comptes de clippers et lance un passage.</p></div>{% else %}
<p class="mut" style="font-size:13px">{{d.n}} vidéo(s) analysée(s). « Effet » au-dessus de 0 = fait mieux que d'habitude.</p>
{% for title, rows in d.tables.items() if rows %}<h2>{{title}}</h2><div class="card"><table>
<tr><th>{{title}}</th><th>Vidéos</th><th>Vues (médiane)</th><th>Effet</th></tr>
{% for r in rows %}<tr><td>{{r.name}}</td><td>{{r.n}}</td><td>{{r.views}}</td>
<td style="color:{{'var(--ok)' if r.effect > 0.05 else ('var(--ko)' if r.effect < -0.05 else 'inherit')}}">{{ '%+.2f'|format(r.effect) }}</td></tr>{% endfor %}
</table></div>{% endfor %}
<h2>Les vidéos qui ont le plus cartonné</h2><div class="card"><table><tr><th>Titre</th><th>Clipper</th><th>Vues</th><th>Durée</th></tr>
{% for t in d.top %}<tr><td>{% if t.from and t.from.startswith('https://') %}<a href="{{t.from}}" target="_blank" rel="noopener">{{t.title}}</a>{% else %}{{t.title}}{% endif %}</td><td>{{t.clipper}}</td><td>{{t.views}}</td><td>{{ (t.len or 0)|int }} s</td></tr>{% endfor %}
</table></div>{% endif %}""", d=d)


@app.route("/youtube-unblock", methods=["POST"])
@login_required
def youtube_unblock():
    f = resolve(load_cfg().get("work_dir", "work")) / "youtube_bloque"
    f.unlink(missing_ok=True)
    flash("Pause YouTube levée : ClipBot réessaiera YouTube au prochain passage.")
    return redirect(url_for("home"))


@app.route("/pause", methods=["POST"])
@login_required
def pause():
    cfg = load_cfg()
    cfg["paused"] = not cfg.get("paused", False)
    save_cfg(cfg)
    flash("⏸ ClipBot est en pause : plus de publication automatique." if cfg["paused"]
          else "▶ Publications automatiques réactivées.")
    return redirect(url_for("home"))


@app.route("/available")
@login_required
def available():
    """File d'attente : les prochains Shorts, dans l'ordre où ClipBot les publiera."""
    cfg = load_cfg()
    error, queue = None, []
    import clipbot
    snap_f = resolve(cfg.get("work_dir", "work")) / "file_snapshot.json"
    fresh = request.args.get("actualiser") == "1"
    try:
        st = clipbot.State(resolve(cfg.get("state_db", "clipbot.db")))
        cands = None
        if not fresh and snap_f.exists() and time.time() - snap_f.stat().st_mtime < 3600:
            try:
                cands = json.loads(snap_f.read_text())   # liste gardée par le dernier passage : immédiat
            except ValueError:
                cands = None
        if cands is None:
            # scan_vods=False : seulement ce qui est déjà analysé (sinon la page mettrait des minutes)
            cands = clipbot.twitch_candidates(cfg, st, scan_vods=False)
            snap_f.parent.mkdir(parents=True, exist_ok=True)
            snap_f.write_text(json.dumps(cands, ensure_ascii=False, default=str))
        try:
            perf = json.loads((resolve(cfg.get("work_dir", "work")) / "performances.json").read_text()).get("effects") or {}
        except (OSError, ValueError):
            perf = {}
        clipbot.apply_perf(cands, perf)
        queue = clipbot.order_candidates(cfg, st, cands, int((cfg.get("reliability") or {}).get("max_tries", 3)))
    except Exception as e:  # clés Twitch absentes, réseau…
        error = str(e)[:300]
    q = clipbot.load_queue(cfg)
    top = set(q["top"])
    skipped = [{"source_id": sid, **(q["info"].get(sid) or {"channel": "", "title": sid})}
               for sid in reversed(q["skip"][-30:])]
    kinds = clipbot.KIND_NAMES
    return page("""<h1>File d'attente</h1>
<p class="mut">Les prochains Shorts, dans l'ordre où ClipBot les publiera (un à chaque passage). ⬆ met un clip en tête,
✖ le retire, « Publier maintenant » le publie tout de suite (sans tenir compte des limites).
<a href="{{url_for('available', actualiser=1)}}">↻ Actualiser</a></p>
{% if error %}<div class="card err">Impossible de lire Twitch : {{error}}</div>{% endif %}
<div class="card">{% if queue %}<table><tr><th>#</th><th>Streamer</th><th>Titre</th><th>Type</th><th></th></tr>
{% for r in queue[:40] %}<tr><td>{{loop.index}}{% if r.source_id in top %} ⭐{% endif %}</td><td>{{r.channel}}</td>
<td><a href="{{r.url}}{% if r.start %}{{ '&' if '?' in r.url else '?' }}t={{ r.start|int }}{% endif %}" target="_blank" rel="noopener">{{r.title}}</a>
{% if r.start is not none and r.start %}<br><span class="mut" style="font-size:12px">à {{ (r.start|int)//3600 }}h{{ '%02d'|format(((r.start|int)%3600)//60) }} · {{ ((r.end or r.start)-r.start)|int }} s</span>{% endif %}</td>
<td style="font-size:13px">{{ kinds.get(r.kind, r.platform) }}{% if r.views %}<br><span class="mut">{{r.views}} vues</span>{% endif %}</td>
<td><div class="row" style="gap:6px;flex-wrap:wrap">
<form method="post" action="{{url_for('queue_action')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="sid" value="{{r.source_id}}"><input type="hidden" name="act" value="top"><button class="sm sec" title="En tête">⬆</button></form>
<form method="post" action="{{url_for('queue_action')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="sid" value="{{r.source_id}}"><input type="hidden" name="act" value="skip"><input type="hidden" name="channel" value="{{r.channel}}"><input type="hidden" name="title" value="{{r.title}}"><button class="sm sec" title="Retirer">✖</button></form>
<form method="post" action="{{url_for('run')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="mode" value="one"><input type="hidden" name="sid" value="{{r.source_id}}"><button class="sm">Publier maintenant</button></form>
</div></td></tr>{% endfor %}</table>{% elif not error %}<p class="mut">Rien en attente pour le moment (les lives sont analysés au prochain passage).</p>{% endif %}</div>
{% if skipped %}<h2>Retirés</h2><div class="card"><table>{% for r in skipped %}<tr><td>{{r.channel}}</td><td>{{r.title}}</td>
<td><form method="post" action="{{url_for('queue_action')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="sid" value="{{r.source_id}}"><input type="hidden" name="act" value="restore"><button class="sm sec">Remettre</button></form></td></tr>{% endfor %}</table></div>{% endif %}
""", queue=queue, error=error, top=top, skipped=skipped, kinds=kinds)


@app.route("/queue", methods=["POST"])
@login_required
def queue_action():
    import clipbot
    cfg = load_cfg()
    sid, act = request.form.get("sid", ""), request.form.get("act")
    if not SID_RE.match(sid) or act not in ("top", "skip", "restore"):
        abort(400)
    q = clipbot.load_queue(cfg)
    q["top"] = [x for x in q["top"] if x != sid]
    q["skip"] = [x for x in q["skip"] if x != sid]
    if act == "top":
        q["top"].insert(0, sid)
        flash("⬆ Mis en tête : ce sera le prochain Short publié.")
    elif act == "skip":
        q["skip"].append(sid)
        q.setdefault("info", {})[sid] = {"channel": request.form.get("channel", "")[:60],
                                         "title": request.form.get("title", "")[:150]}
        flash("✖ Retiré de la file d'attente.")
    else:
        flash("Remis dans la file d'attente.")
    clipbot.save_queue(cfg, q)
    return redirect(url_for("available"))


# --------------------------------------------------------------------------
# Historique
# --------------------------------------------------------------------------
@app.route("/history")
@login_required
def history():
    status = request.args.get("status", "")
    con = db()
    sql, args = "SELECT * FROM clips", []
    if status:
        sql, args = sql + " WHERE status=?", [status]
    rows = con.execute(sql + " ORDER BY updated DESC LIMIT 300", args).fetchall()
    return page("""<h1>Historique</h1>
<div class="row" style="flex:none;margin-bottom:12px">
<a class="btn {{'' if not st else 'sec'}}" href="?">Tout</a>
<a class="btn {{'' if st=='done' else 'sec'}}" href="?status=done">Publiés</a>
<a class="btn {{'' if st=='error' else 'sec'}}" href="?status=error">Erreurs</a></div>
<div class="card">""" + HISTORY_TABLE + "</div>", rows=rows, st=status)


@app.route("/clip", methods=["POST"])
@login_required
def clip_action():
    sid, action = request.form["sid"], request.form["a"]
    con = db()
    if action == "retry":
        con.execute("UPDATE clips SET tries=0, status='error' WHERE source_id=?", (sid,))
        flash("Le clip sera retenté au prochain passage.")
    elif action == "forget":
        con.execute("DELETE FROM clips WHERE source_id=?", (sid,))
        flash("Clip oublié.")
    con.commit()
    return safe_back()


# --------------------------------------------------------------------------
# Chaînes autorisées
# --------------------------------------------------------------------------
@app.route("/channels", methods=["GET", "POST"])
@login_required
def channels():
    cfg = load_cfg()
    src = cfg.setdefault("sources", {})
    tw = src.setdefault("twitch", {"enabled": False, "channels": []})
    yt = src.setdefault("youtube", {"enabled": False, "channels": []})
    if request.method == "POST":
        f = request.form
        tw_list = []
        rediffs = f.getlist("tw_rediff")
        for k, (login, mv) in enumerate(zip(f.getlist("tw_login"), f.getlist("tw_min"))):
            login = login.strip().lstrip("@").lower()
            if login:
                row = {"login": login, **({"min_views": int(mv)} if mv.strip().isdigit() else {})}
                rd = (rediffs[k] if k < len(rediffs) else "").strip()
                if rd.startswith("@"):
                    rd = "https://www.youtube.com/" + rd
                if rd.lower() in ("non", "aucune"):
                    row["rediffs"] = "non"
                elif re.fullmatch(r"https://(www\.|m\.)?youtube\.com/(@[\w.\-]+|channel/[\w\-]+|c/[\w.\-]+)(/videos|/streams)?/?", rd):
                    row["rediffs"] = rd
                tw_list.append(row)
        yt_list = []
        for url, name in zip(f.getlist("yt_url"), f.getlist("yt_name")):
            if url.strip():
                yt_list.append({"url": url.strip(), "name": name.strip() or url.strip()})
        tw["channels"], yt["channels"] = tw_list, yt_list
        tw["enabled"], yt["enabled"] = "tw_on" in f, "yt_on" in f
        save_cfg(cfg)
        flash("Chaînes enregistrées ✅")
        return redirect(url_for("channels"))
    tw_rows = [c if isinstance(c, dict) else {"login": c} for c in tw.get("channels") or []]
    yt_rows = [c if isinstance(c, dict) else {"url": c} for c in yt.get("channels") or []]
    return page("""<h1>Chaînes autorisées</h1>
<p class="mut">⚠️ N'ajoute que des créateurs qui t'ont donné leur autorisation (garde une preuve écrite).</p>
<form method="post"><input type="hidden" name="csrf" value="{{csrf_token()}}">
<div class="card"><h2 style="margin-top:0">Twitch <label style="display:inline;margin-left:10px"><input type="checkbox" name="tw_on" style="width:auto" {{'checked' if tw.enabled}}> activé</label></h2>
<div id="tw">{% for c in tw_rows + [{}] %}<div class="row" style="margin-bottom:6px">
<input name="tw_login" placeholder="pseudo Twitch" value="{{c.login or ''}}">
<input name="tw_min" placeholder="vues min. (défaut {{tw.min_views or 0}})" value="{{c.min_views or ''}}" style="max-width:220px">
<input name="tw_rediff" placeholder="{{ ('trouvée auto : ' ~ auto[c.login].name) if c.login and auto.get(c.login, {}).get('url') else 'rediffs YouTube : auto (ou lien, ou « non »)' }}" value="{{c.rediffs or ''}}"></div>{% endfor %}</div>
<p class="mut" style="font-size:13px">Ajoute juste le pseudo : ClipBot regarde Twitch, et si les rediffusions y sont absentes ou réservées aux abonnés, il cherche tout seul la chaîne YouTube où le streamer republie ses lives (le nom trouvé s'affiche dans le champ). Tu peux aussi coller le lien toi-même, ou écrire « non » pour ne pas utiliser YouTube.</p>
{% for login, a in auto.items() if a.url %}<p class="mut" style="font-size:12px;margin:2px 0">🔎 {{login}} → <a href="{{a.url}}" target="_blank" rel="noopener">{{a.name}}</a></p>{% endfor %}
<button type="button" class="sec sm" onclick="add('tw')">+ Ajouter</button></div>
<div class="card"><h2 style="margin-top:0">YouTube <label style="display:inline;margin-left:10px"><input type="checkbox" name="yt_on" style="width:auto" {{'checked' if yt.enabled}}> activé</label></h2>
<div id="yt">{% for c in yt_rows + [{}] %}<div class="row" style="margin-bottom:6px">
<input name="yt_url" placeholder="https://www.youtube.com/@Chaine/videos" value="{{c.url or ''}}">
<input name="yt_name" placeholder="nom affiché" value="{{c.name or ''}}" style="max-width:220px"></div>{% endfor %}</div>
<button type="button" class="sec sm" onclick="add('yt')">+ Ajouter</button></div>
<p class="mut">Pour retirer une chaîne, vide sa ligne puis enregistre.</p>
<button>Enregistrer</button></form>
<script>function add(id){const box=document.getElementById(id),r=box.lastElementChild.cloneNode(true);
r.querySelectorAll('input').forEach(i=>i.value='');box.appendChild(r)}</script>""",
                tw=tw, yt=yt, tw_rows=tw_rows, yt_rows=yt_rows, auto=auto_rediffs(cfg))


# --------------------------------------------------------------------------
# Réglages
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    cfg = load_cfg()
    tw = cfg["sources"].setdefault("twitch", {})
    pub, v, meta = cfg["publish"], cfg["video"], cfg["metadata"]
    ntfy = cfg.setdefault("ntfy", {})
    if request.method == "POST":
        f = request.form
        num = lambda k, d: int(f.get(k) or d)
        cfg["max_posts_per_run"] = max(1, num("per_run", 1))
        cfg["max_posts_per_day"] = max(1, num("per_day", 12))
        sch = cfg.setdefault("schedule", {})
        sch["interval_minutes"] = max(15, min(num("interval", 60), 1440))
        sch["start_hour"] = max(0, min(num("start_hour", 11), 23))
        sch["end_hour"] = max(sch["start_hour"] + 1, min(num("end_hour", 22), 24))
        cfg["rotation"] = "rotation" in f
        v["max_duration"] = num("max_duration", 59)
        v.setdefault("subtitles", {})["enabled"] = "subs" in f
        v["layout"] = "bandeau" if f.get("layout") == "bandeau" else "flou"
        bd = v.setdefault("bandeau", {})
        for k, d in (("line1", "ABONNEZ-VOUS"), ("line2", "ET LIKEZ !"), ("footer", "REDIFSTREAM")):
            bd[k] = (f.get("bd_" + k) or d).strip()[:40]
        lg = v.setdefault("logo", {"file": "assets/logo.png"})
        lg["enabled"] = "logo_on" in f
        lg["position"] = "bottom" if f.get("logo_pos") == "bottom" else "top"
        lg["width"] = max(80, min(num("logo_width", 220), 600))
        tw["lookback_hours"] = min(num("lookback", 48), 168)
        tw["games"] = [g.strip() for g in f.get("games", "").split(",") if g.strip()]
        vd = tw.setdefault("vods", {})
        vd["enabled"] = "vods_on" in f
        vd["chat"] = "vods_chat" in f
        vd["deep_listen"] = "vods_deep" in f
        hk = v.setdefault("hook", {})
        hk["enabled"], hk["teaser"], hk["text_banner"] = "hook_on" in f, "hook_teaser" in f, "hook_text" in f
        hk.pop("text", None)
        hk["fallback"] = f.get("hook_fallback", "").strip()[:44]
        vd["follow_trends"] = "vods_trends" in f
        vd["deep_max_per_run"] = max(0, min(int(f.get("vods_deep_max") or 12), 60))
        vd["highlights_per_vod"] = max(1, min(int(f.get("vods_per") or 6), 30))
        vd["max_vods"] = max(1, min(int(f.get("vods_max") or 2), 10))
        vd["min_gap_seconds"] = max(30, min(int(f.get("vods_gap") or 120), 3600))
        vd["title_keywords"] = [k.strip() for k in f.get("vods_kw", "").split(",") if k.strip()]
        cl = cfg["sources"].setdefault("clippers", {})
        cl["enabled"] = "cl_on" in f
        cl["lookback_hours"] = max(6, min(int(f.get("cl_lookback") or 72), 336))
        cl["per_channel"] = max(1, min(int(f.get("cl_per") or 10), 50))
        urls = []
        for u in re.split(r"[\s,]+", f.get("cl_channels", "")):
            u = u.strip()
            if u.startswith("@"):
                u = "https://www.youtube.com/" + u
            if re.fullmatch(r"https://(www\.|m\.)?youtube\.com/(@[\w.\-]+|channel/[\w\-]+|c/[\w.\-]+)(/shorts|/videos)?/?", u) \
                    or re.fullmatch(r"https://(www\.)?tiktok\.com/@[\w.\-]+/?", u):
                urls.append(u)
        cl["channels"] = urls[:30]
        meta["fallback_title"] = f.get("fallback_title", "").strip() or "Moment fou en live"
        tw["min_views"] = num("min_views", 0)
        if f.get("tw_id"):
            tw["client_id"] = f["tw_id"].strip()
        if f.get("tw_secret"):
            tw["client_secret"] = f["tw_secret"].strip()
        for p in ("youtube", "tiktok"):
            pub[p]["enabled"] = f"{p}_on" in f
            pub[p]["method"] = f.get(f"{p}_method", "browser")
        pub["youtube"]["privacy"] = f.get("yt_privacy", "public")
        meta["title"] = f.get("title", meta["title"])
        meta["spoken_titles"] = "spoken_titles" in f
        meta["description"] = f.get("description", meta["description"]).replace("\r\n", "\n")
        meta["tiktok_caption"] = f.get("tiktok_caption", meta.get("tiktok_caption", ""))
        yurl = f.get("youtube_url", "").strip()
        if yurl.startswith("https://www.youtube.com/") or yurl.startswith("https://youtube.com/"):
            cfg["youtube_url"] = yurl
        turl = f.get("tiktok_url", "").strip()
        if turl.startswith("@"):
            turl = "https://www.tiktok.com/" + turl
        if not turl:
            cfg.pop("tiktok_url", None)
        elif re.fullmatch(r"https://(www\.)?tiktok\.com/@[\w.\-]+/?", turl):
            cfg["tiktok_url"] = turl
        else:
            flash("Lien TikTok ignoré : il doit ressembler à https://www.tiktok.com/@tonpseudo")
        ntfy["url"], ntfy["topic"] = f.get("ntfy_url", "").strip(), f.get("ntfy_topic", "").strip()
        if ntfy["url"] and "://" not in ntfy["url"]:
            ntfy["url"] = "https://" + ntfy["url"]          # « ny.leo44.cloud » -> « https://ny.leo44.cloud »
        if f.get("ntfy_token"):
            ntfy["token"] = f["ntfy_token"].strip()
        # --- Réglages avancés
        def clamp(k, d, lo, hi):
            try:
                return max(lo, min(int(f.get(k) or d), hi))
            except ValueError:
                return d
        sub = v.setdefault("subtitles", {})
        sub["model"] = f.get("sub_model") if f.get("sub_model") in ("tiny", "base", "small", "medium") else sub.get("model", "small")
        sub["language"] = f.get("sub_lang") if f.get("sub_lang") in ("fr", "en", "auto") else "fr"
        if sub["language"] == "auto":
            sub["language"] = None
        sub["words_per_line"] = clamp("sub_words", 4, 1, 8)
        v["crf"] = clamp("crf", 21, 16, 30)
        acc = f.get("accent", "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", acc):
            v.setdefault("bandeau", {})["accent"] = acc.upper()
        meta["tags"] = [t.strip() for t in f.get("tags", "").split(",") if t.strip()][:30]
        meta["min_title_length"] = clamp("min_title", 4, 0, 30)
        cfg["max_tries"] = clamp("max_tries", 3, 1, 10)
        ys = cfg["sources"].setdefault("youtube", {})
        ys["lookback_hours"] = clamp("yt_lookback", 72, 1, 720)
        ys["min_age_hours"] = clamp("yt_min_age", 12, 0, 168)
        ys["videos_per_channel"] = clamp("yt_videos", 5, 1, 30)
        ys["clips_per_video"] = clamp("yt_clips", 1, 1, 5)
        pub.setdefault("browser", {})["timeout_s"] = clamp("timeout", 60, 20, 300)
        save_cfg(cfg)
        flash("Réglages enregistrés ✅")
        return redirect(url_for("settings"))
    return page("""<h1>Réglages</h1><form method="post"><input type="hidden" name="csrf" value="{{csrf_token()}}">
<div class="card"><h2 style="margin-top:0">Rythme</h2><div class="row">
<div><label>1 clip toutes les (minutes)</label><input type="number" name="interval" min="15" step="15" value="{{(cfg.schedule or {}).interval_minutes or 60}}"></div>
<div><label>Actif de (heure)</label><input type="number" name="start_hour" min="0" max="23" value="{{(cfg.schedule or {}).start_hour if (cfg.schedule or {}).start_hour is not none else 11}}"></div>
<div><label>à (heure)</label><input type="number" name="end_hour" min="1" max="24" value="{{(cfg.schedule or {}).end_hour or 22}}"></div></div>
<label><input type="checkbox" name="rotation" style="width:auto" {{'checked' if cfg.rotation is not false}}> Alterner entre les créateurs (sinon : toujours le clip le plus vu)</label>
<div class="row">
<div><label>Clips max par passage</label><input type="number" name="per_run" value="{{cfg.max_posts_per_run}}"></div>
<div><label>Clips max par 24 h</label><input type="number" name="per_day" value="{{cfg.max_posts_per_day}}"></div>
<div><label>Durée max (s)</label><input type="number" name="max_duration" value="{{v.max_duration}}"></div></div>
<label><input type="checkbox" name="subs" style="width:auto" {{'checked' if (v.subtitles or {}).enabled}}> Sous-titres automatiques</label></div>

<div class="card"><h2 style="margin-top:0">Design des vidéos</h2>
<label>Mise en page</label><select name="layout">
<option value="bandeau" {{'selected' if v.layout == 'bandeau'}}>A · Bandeau (fond noir, message en haut, gameplay encadré)</option>
<option value="flou" {{'selected' if v.layout != 'bandeau'}}>Fond flou (ancien)</option></select>
<div class="row"><div><label>Ligne 1 du haut</label><input name="bd_line1" maxlength="40" value="{{(v.bandeau or {}).line1 or 'ABONNEZ-VOUS'}}"></div>
<div><label>Ligne 2 (en couleur)</label><input name="bd_line2" maxlength="40" value="{{(v.bandeau or {}).line2 or 'ET LIKEZ !'}}"></div></div>
<label>Pied de page</label><input name="bd_footer" maxlength="40" value="{{(v.bandeau or {}).footer or 'REDIFSTREAM'}}">
<p class="mut" style="font-size:13px">Le logo (envoyé plus bas) s'affiche en haut à gauche dans ce design.</p></div>

<div class="card"><h2 style="margin-top:0">Logo dans la vidéo</h2>
<label><input type="checkbox" name="logo_on" style="width:auto" {{'checked' if (v.logo or {}).enabled}}> Afficher mon logo</label>
<div class="row"><div><label>Position</label><select name="logo_pos">
<option value="top" {{'selected' if (v.logo or {}).position != 'bottom'}}>En haut</option>
<option value="bottom" {{'selected' if (v.logo or {}).position == 'bottom'}}>En bas</option></select></div>
<div><label>Largeur (px, la vidéo fait 1080 de large)</label><input type="number" name="logo_width" value="{{(v.logo or {}).width or 220}}"></div></div>
<p class="mut" style="font-size:13px">{{ 'Logo actuel :' if logo_ok else 'Aucun logo envoyé : ajoute-le dans la case ci-dessous.' }}</p>
{% if logo_ok %}<img src="{{url_for('logo_file')}}" alt="Logo actuel" style="width:120px;border-radius:12px;border:1px solid var(--line)">{% endif %}</div>

<div class="card"><h2 style="margin-top:0">Twitch</h2><div class="row">
<div><label>Client ID {{'(déjà rempli)' if tw.client_id and tw.client_id!='xxxxxxxx'}}</label><input name="tw_id" placeholder="laisser vide pour ne pas changer"></div>
<div><label>Client secret {{'(déjà rempli)' if tw.client_secret and tw.client_secret!='xxxxxxxx'}}</label><input name="tw_secret" type="password" placeholder="laisser vide pour ne pas changer"></div></div>
<div class="row"><div><label>Clips des dernières (heures)</label><input type="number" name="lookback" value="{{tw.lookback_hours or 48}}"></div>
<div><label>Vues minimum par défaut</label><input type="number" name="min_views" value="{{tw.min_views or 0}}"></div></div>
<label>Jeux autorisés (noms Twitch séparés par des virgules, vide = tous)</label>
<input name="games" value="{{ (tw.games or [])|join(', ') }}" placeholder="Grand Theft Auto V, Grand Theft Auto VI"></div>

<div class="card"><h2 style="margin-top:0">Moments forts des lives (rediffusions)</h2>
<p class="mut" style="font-size:13px">En plus des clips des viewers, ClipBot analyse les rediffusions et repère les moments forts, sans jamais reprendre un passage publié. Il apprend tout seul à les reconnaître à partir des passages déjà clippés par les viewers et les clippers (volume, cris, rires, rythme de la voix…).</p>
<p style="font-size:13px">🧠 {{ model_info }}</p>
<label><input type="checkbox" name="vods_on" style="width:auto" {{'checked' if (tw.vods or {}).enabled}}> Activer</label>
<label><input type="checkbox" name="vods_chat" style="width:auto" {{'checked' if (tw.vods or {}).get('chat', True)}}> Analyser aussi le chat (replay du chat YouTube, ou chat incrusté dans la rediff)</label>
<label><input type="checkbox" name="vods_trends" style="width:auto" {{'checked' if (tw.vods or {}).get('follow_trends', True)}}> Suivre les tendances des clippers (durée des extraits qui marche le mieux, voir 🔥 Tendances)</label>
<label><input type="checkbox" name="vods_deep" style="width:auto" {{'checked' if (tw.vods or {}).get('deep_listen', True)}}> Écoute approfondie : transcrire les meilleurs passages (réactions, rires) et couper entre deux phrases</label>
<label>Passages écoutés en profondeur par lancement</label><input type="number" name="vods_deep_max" min="0" max="60" value="{{ (tw.vods or {}).get('deep_max_per_run', 12) }}">
<div class="row"><div><label>Extraits max par live</label><input type="number" name="vods_per" min="1" max="30" value="{{(tw.vods or {}).highlights_per_vod or 6}}"></div>
<div><label>Lives regardés par chaîne</label><input type="number" name="vods_max" min="1" max="10" value="{{(tw.vods or {}).max_vods or 2}}"></div>
<div><label>Écart entre 2 extraits (s)</label><input type="number" name="vods_gap" min="30" value="{{(tw.vods or {}).min_gap_seconds or 120}}"></div></div>
<label>Seulement les lives dont le titre contient (virgules, vide = tous)</label>
<input name="vods_kw" value="{{ ((tw.vods or {}).title_keywords or [])|join(', ') }}" placeholder="GTA"></div>

<div class="card"><h2 style="margin-top:0">Comptes de clippers (YouTube et TikTok)</h2>
<p class="mut" style="font-size:13px">ClipBot regarde les vidéos récentes de ces comptes, retrouve par le son le passage du live qu'ils ont clippé (même accéléré, avec musique ou coupé), et refait ce moment depuis le live d'origine avec ton design. Ces moments passent en priorité. Ils servent aussi d'exemples pour apprendre à repérer les moments forts. Nécessite « Moments forts des lives » activé.</p>
{% set cl = cfg.sources.clippers or {} %}
<label><input type="checkbox" name="cl_on" style="width:auto" {{'checked' if cl.enabled}}> Activer</label>
<label>Comptes (un par ligne : lien YouTube, lien TikTok ou @pseudo YouTube)</label>
<textarea name="cl_channels" rows="4" placeholder="https://www.youtube.com/@NomDuClipper&#10;https://www.tiktok.com/@nomduclipper">{{ (cl.channels or [])|join('\n') }}</textarea>
<div class="row"><div><label>Shorts récents de moins de (heures)</label><input type="number" name="cl_lookback" min="6" max="336" value="{{cl.lookback_hours or 72}}"></div>
<div><label>Shorts regardés par chaîne</label><input type="number" name="cl_per" min="1" max="50" value="{{cl.per_channel or 10}}"></div></div></div>

<div class="card"><h2 style="margin-top:0">Publication</h2>
{% for p,n in [('youtube','YouTube Shorts'),('tiktok','TikTok')] %}<div class="row" style="margin-bottom:8px">
<label style="flex:0 0 auto;min-width:200px;margin:0"><input type="checkbox" name="{{p}}_on" style="width:auto" {{'checked' if pub[p].enabled}}> {{n}}</label>
<select name="{{p}}_method"><option value="browser" {{'selected' if pub[p].method=='browser'}}>via le navigateur</option>
<option value="api" {{'selected' if pub[p].method=='api'}}>via l'API officielle</option></select></div>{% endfor %}
<div class="mut" style="font-size:13px;margin:6px 0 10px">Pour ajouter ton compte TikTok : 1) connecte-le avec
<a href="{{url_for('tiktok_login')}}">📱 Connexion TikTok (QR code)</a>, depuis ton téléphone — ou envoie ses cookies dans
<a href="{{url_for('cookies')}}">Cookies → TikTok</a> depuis un PC ;
2) sur l'accueil, « Vérifier les connexions » ; 3) coche TikTok ci-dessus et Enregistrer.
{% if tiktok_cookies %}<b style="color:var(--ok)">✅ Cookies TikTok présents</b>{% else %}<b style="color:var(--ko)">❌ Cookies TikTok absents</b>{% endif %}</div>
<label>Lien de ton compte TikTok (bouton « Voir sur TikTok »)</label>
<input name="tiktok_url" value="{{cfg.tiktok_url or ''}}" placeholder="https://www.tiktok.com/@redifstream">
<label>Lien de ta chaîne YouTube (bouton « Voir sur YouTube »)</label>
<input name="youtube_url" value="{{cfg.youtube_url or 'https://www.youtube.com/@redifstream2'}}">
<label>Visibilité YouTube</label><select name="yt_privacy">{% for o,l in [('public','Public'),('unlisted','Non répertoriée'),('private','Privée')] %}
<option value="{{o}}" {{'selected' if pub.youtube.privacy==o}}>{{l}}</option>{% endfor %}</select></div>

<div class="card"><h2 style="margin-top:0">Textes</h2><p class="mut">Variables : {title} {channel} {url} {credit_url}</p>
<label><input type="checkbox" name="spoken_titles" style="width:auto" {{'checked' if meta.get('spoken_titles', True)}}> Titre tiré de ce qui est dit : la réplique la plus forte remplace {title}, au style qui marche dans les Tendances</label>
<label>Titre YouTube</label><input name="title" value="{{meta.title}}">
<label>Titre de secours (si le titre du clip est trop court, ex. « x »)</label><input name="fallback_title" value="{{meta.fallback_title or 'Moment fou en live'}}">
<label>Description YouTube</label><textarea name="description" rows="6">{{meta.description}}</textarea>
<label>Légende TikTok</label><input name="tiktok_caption" value="{{meta.tiktok_caption}}"></div>

<div class="card"><h2 style="margin-top:0">Accroche (1re seconde)</h2>
{% set hk = v.hook or {} %}
<label><input type="checkbox" name="hook_on" style="width:auto" {{'checked' if hk.get('enabled', True)}}> Activer l'accroche</label>
<label><input type="checkbox" name="hook_teaser" style="width:auto" {{'checked' if hk.get('teaser', True)}}> Teaser : montrer d'abord les ~3 s les plus fortes, puis repartir du début</label>
<label><input type="checkbox" name="hook_text" style="width:auto" {{'checked' if hk.get('text_banner', False)}}> Bandeau jaune avec la réplique la plus forte pendant 2,5 s (désactivé par défaut)</label>
<label>Texte de secours (si aucune réplique forte, vide = rien)</label><input name="hook_fallback" maxlength="44" value="{{hk.fallback or ''}}" placeholder="REGARDE JUSQU'À LA FIN"></div>

<div class="card"><h2 style="margin-top:0">Sous-titres et qualité</h2><div class="row">
<div><label>Précision des sous-titres</label><select name="sub_model">
{% for m,l in [('tiny','Très rapide (moins précis)'),('base','Rapide'),('small','Équilibré (conseillé)'),('medium','Très précis (lent)')] %}
<option value="{{m}}" {{'selected' if (v.subtitles or {}).model == m or (m == 'small' and not (v.subtitles or {}).model)}}>{{l}}</option>{% endfor %}</select></div>
<div><label>Langue parlée</label><select name="sub_lang">
<option value="fr" {{'selected' if (v.subtitles or {}).language == 'fr'}}>Français</option>
<option value="en" {{'selected' if (v.subtitles or {}).language == 'en'}}>Anglais</option>
<option value="auto" {{'selected' if (v.subtitles or {}).language is none}}>Détection automatique</option></select></div>
<div><label>Mots par ligne</label><input type="number" name="sub_words" min="1" max="8" value="{{(v.subtitles or {}).words_per_line or 4}}"></div></div>
<div class="row"><div><label>Couleur du design (jaune par défaut)</label><input type="color" name="accent" value="{{(v.bandeau or {}).accent or '#FFD21F'}}" style="height:42px"></div>
<div><label>Qualité vidéo (16 = meilleure, 30 = plus légère)</label><input type="number" name="crf" min="16" max="30" value="{{v.crf or 21}}"></div></div></div>

<div class="card"><h2 style="margin-top:0">YouTube : tags et titres</h2>
<label>Tags (séparés par des virgules, utilisés seulement en mode API)</label><input name="tags" value="{{ (meta.tags or [])|join(', ') }}">
<label>Longueur minimale d'un titre de clip (en dessous, le titre de secours est utilisé)</label><input type="number" name="min_title" min="0" max="30" value="{{meta.min_title_length if meta.min_title_length is not none else 4}}"></div>

<div class="card"><h2 style="margin-top:0">Source YouTube (si tu ajoutes des chaînes YouTube)</h2><div class="row">
<div><label>Vidéos publiées depuis moins de (h)</label><input type="number" name="yt_lookback" value="{{(cfg.sources.youtube or {}).lookback_hours or 72}}"></div>
<div><label>…et depuis plus de (h)</label><input type="number" name="yt_min_age" value="{{(cfg.sources.youtube or {}).min_age_hours if (cfg.sources.youtube or {}).min_age_hours is not none else 12}}"></div>
<div><label>Vidéos regardées par chaîne</label><input type="number" name="yt_videos" value="{{(cfg.sources.youtube or {}).videos_per_channel or 5}}"></div>
<div><label>Extraits par vidéo</label><input type="number" name="yt_clips" value="{{(cfg.sources.youtube or {}).clips_per_video or 1}}"></div></div></div>

<div class="card"><h2 style="margin-top:0">Fiabilité</h2><div class="row">
<div><label>Essais avant d'abandonner un clip</label><input type="number" name="max_tries" min="1" max="10" value="{{cfg.max_tries or 3}}"></div>
<div><label>Délai max d'une page YouTube (secondes)</label><input type="number" name="timeout" min="20" max="300" value="{{(pub.browser or {}).timeout_s or 60}}"></div></div></div>

<div class="card"><h2 style="margin-top:0">Notifications ntfy</h2><div class="row">
<div><label>URL du serveur</label><input name="ntfy_url" value="{{ntfy.url or ''}}"></div>
<div><label>Topic</label><input name="ntfy_topic" value="{{ntfy.topic or ''}}"></div>
<div><label>Jeton {{'(déjà rempli)' if ntfy.token}}</label><input name="ntfy_token" type="password" placeholder="laisser vide pour ne pas changer"></div></div>
<p class="mut" style="font-size:13px">Une notification à chaque vidéo publiée (touche-la pour ouvrir le Short) et à chaque erreur.</p>
<button class="sec sm" formaction="{{url_for('ntfy_test')}}" formmethod="post">🔔 Envoyer une notification de test</button></div>
<button>Enregistrer</button></form>
<div class="card" style="margin-top:16px"><h2 style="margin-top:0">Envoyer mon logo (PNG ou JPG, 2 Mo max)</h2>
<form method="post" action="{{url_for('logo_upload')}}" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{csrf_token()}}">
<input type="file" name="file" accept="image/png,image/jpeg"><br><br><button class="sec">Envoyer le logo</button></form></div>
<div class="card" style="margin-top:16px"><h2 style="margin-top:0">Changer le mot de passe de l'interface</h2>
<form method="post" action="{{url_for('password')}}"><input type="hidden" name="csrf" value="{{csrf_token()}}">
<div class="row"><div><label>Mot de passe actuel</label><input type="password" name="current" autocomplete="current-password"></div>
<div><label>Nouveau (10 caractères min.)</label><input type="password" name="new" autocomplete="new-password"></div>
<div><label>Confirmer</label><input type="password" name="confirm" autocomplete="new-password"></div></div><br>
<button class="sec">Changer le mot de passe</button></form>
<p class="mut" style="font-size:13px">Accès réservé à : {{ (cfg.web or {}).allowed_ips|join(', ') if (cfg.web or {}).allowed_ips else 'tout le monde' }} · double authentification : {{ 'activée' if totp_on else 'désactivée' }}.</p></div>""",
                cfg=cfg, v=v, tw=tw, pub=pub, meta=meta, ntfy=ntfy, logo_ok=logo_path().exists(),
                model_info=model_info(cfg),
                tiktok_cookies=resolve((pub.get("browser") or {}).get("tiktok_cookies", "secrets/tiktok_cookies.txt")).exists(),
                totp_on=TOTP_FILE.exists())


@app.route("/password", methods=["POST"])
@login_required
def password():
    f = request.form
    if not check_password_hash(PASS_FILE.read_text().strip(), f.get("current", "")):
        flash("Mot de passe actuel incorrect.")
    elif len(f.get("new", "")) < 10 or f.get("new") != f.get("confirm"):
        flash("Le nouveau mot de passe doit faire 10 caractères minimum et être confirmé à l'identique.")
    else:
        PASS_FILE.write_text(generate_password_hash(f["new"]))
        PASS_FILE.chmod(0o600)
        flash("Mot de passe changé ✅")
    return redirect(url_for("settings"))


def logo_path():
    lg = (load_cfg().get("video") or {}).get("logo") or {}
    return resolve(lg.get("file", "assets/logo.png"))


@app.route("/logo", methods=["POST"])
@login_required
def logo_upload():
    up = request.files.get("file")
    data = up.read() if up else b""
    is_png, is_jpg = data[:8] == b"\x89PNG\r\n\x1a\n", data[:3] == b"\xff\xd8\xff"
    if not (is_png or is_jpg):
        flash("Envoie une image PNG ou JPG.")
        return redirect(url_for("settings"))
    p = logo_path()
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg") or ".." in p.parts:
        abort(400)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    cfg = load_cfg()
    cfg.setdefault("video", {}).setdefault("logo", {"file": "assets/logo.png"})["enabled"] = True
    save_cfg(cfg)
    flash("Logo enregistré et activé ✅ Il apparaîtra dans les prochains clips.")
    return redirect(url_for("settings"))


@app.route("/logo.png")
@login_required
def logo_file():
    p = logo_path()
    if not p.exists():
        abort(404)
    return send_from_directory(p.parent, p.name)


# --------------------------------------------------------------------------
# Cookies
# --------------------------------------------------------------------------
@app.route("/cookies", methods=["GET", "POST"])
@login_required
def cookies():
    b = load_cfg()["publish"].setdefault("browser", {})
    files = {"tiktok": resolve(b.get("tiktok_cookies", "secrets/tiktok_cookies.txt")),
             "youtube": resolve(b.get("youtube_cookies", "secrets/youtube_cookies.txt")),
             "telechargement": resolve("secrets/ytdlp_cookies.txt")}
    if request.method == "POST":
        which = request.form.get("which")
        if which not in files:
            abort(400)
        text = request.form.get("text", "").strip()
        up = request.files.get("file")
        if up and up.filename:
            text = up.read().decode("utf-8", errors="ignore").strip()
        if "\t" not in text:
            flash("Ça ne ressemble pas à un fichier cookies.txt (format Netscape).")
        else:
            files[which].parent.mkdir(exist_ok=True)
            files[which].write_text(text + "\n", encoding="utf-8")
            files[which].chmod(0o600)
            if which == "telechargement":
                cfg = load_cfg()
                cfg["ytdlp_cookies"] = "secrets/ytdlp_cookies.txt"
                save_cfg(cfg)
                flash("Cookies de téléchargement enregistrés ✅ Ils servent à télécharger les rediffs YouTube.")
            else:
                flash(f"Cookies {which} enregistrés ✅ Lance « Vérifier les connexions » pour tester.")
        return redirect(url_for("cookies"))
    info = {k: (datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M") if p.exists() else None)
            for k, p in files.items()}
    return page("""<h1>Cookies</h1>
<p class="mut">Exporte-les depuis Chrome avec l'extension « Get cookies.txt LOCALLY », une fois connecté au site. Ils ne sont jamais réaffichés ici.</p>
{% for k,n in [('tiktok','TikTok'),('youtube','YouTube (publication)'),('telechargement','Téléchargements YouTube (rediffs)')] %}
<div class="card"><h2 style="margin-top:0">{{n}} — {{'✅ mis à jour le ' ~ info[k] if info[k] else '❌ absent'}}</h2>
<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{{csrf_token()}}"><input type="hidden" name="which" value="{{k}}">
{% if k == 'telechargement' %}<p class="mut" style="font-size:13px">Seulement si YouTube bloque les téléchargements (« confirm you're not a bot »). Utilise un <b>autre compte Google</b> que RedifStream : YouTube peut déconnecter un compte utilisé pour télécharger. Exporte-les en navigation privée, puis ferme la fenêtre sans te déconnecter.</p>{% endif %}
<label>Fichier cookies.txt</label><input type="file" name="file" accept=".txt">
<label>…ou colle son contenu</label><textarea name="text" rows="4"></textarea><br><br><button>Enregistrer</button></form></div>{% endfor %}""",
                info=info)


# --------------------------------------------------------------------------
# Captures, aperçus et logs
# --------------------------------------------------------------------------
MEDIA_DIRS = {"screenshots": BASE / "screenshots", "preview": BASE / "preview"}


@app.route("/media")
@login_required
def media():
    lists = {k: sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:40] if d.exists() else []
             for k, d in MEDIA_DIRS.items()}
    return page("""<h1>Captures & aperçus</h1>
<h2>Aperçus (essais sans publier)</h2><div class="media">
{% for f in lists.preview %}<div><video src="{{url_for('media_file',kind='preview',name=f.name)}}" controls preload="metadata"></video><div class="mut" style="font-size:12px">{{f.name}}</div></div>
{% else %}<p class="mut">Aucun aperçu.</p>{% endfor %}</div>
<h2>Captures d'écran (vérifications et erreurs)</h2><div class="media">
{% for f in lists.screenshots %}<a href="{{url_for('media_file',kind='screenshots',name=f.name)}}" target="_blank"><img src="{{url_for('media_file',kind='screenshots',name=f.name)}}" loading="lazy"><div class="mut" style="font-size:12px">{{f.name}}</div></a>
{% else %}<p class="mut">Aucune capture.</p>{% endfor %}</div>
<form method="post" action="{{url_for('media_clear')}}" style="margin-top:20px"><input type="hidden" name="csrf" value="{{csrf_token()}}"><button class="ko">Tout effacer</button></form>""",
                lists=lists)


@app.route("/media/<kind>/<path:name>")
@login_required
def media_file(kind, name):
    if kind not in MEDIA_DIRS:
        abort(404)
    return send_from_directory(MEDIA_DIRS[kind], name)


@app.route("/media/clear", methods=["POST"])
@login_required
def media_clear():
    for d in MEDIA_DIRS.values():
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
    flash("Captures et aperçus effacés.")
    return redirect(url_for("media"))


@app.route("/logs")
@login_required
def logs():
    return page("""<h1>Logs</h1>
<h2>Dernier lancement depuis l'interface {{'⏳ en cours' if running else ''}}</h2><pre id="job">{{job or 'Aucun.'}}</pre>
<h2>Journal ClipBot</h2><pre>{{main}}</pre>
{% if running %}<script>setTimeout(()=>location.reload(),4000)</script>{% endif %}""",
                job=tail(JOB_LOG, 150), main=tail(BASE / "logs" / "clipbot.log", 300),
                running=job_running())


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Interface web ClipBot")
    ap.add_argument("--set-password", action="store_true")
    ap.add_argument("--setup-2fa", action="store_true", help="active la double authentification")
    ap.add_argument("--disable-2fa", action="store_true")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    SECRETS.mkdir(exist_ok=True)
    SECRETS.chmod(0o700)
    if args.disable_2fa:
        TOTP_FILE.unlink(missing_ok=True)
        print("Double authentification désactivée.")
        return
    if args.setup_2fa:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        uri = f"otpauth://totp/ClipBot:admin?secret={secret}&issuer=ClipBot"
        print("\nAjoute ce compte dans ton appli d'authentification (Google Authenticator, Aegis, Bitwarden…)")
        print(f"  Clé à saisir : {secret}")
        print(f"  ou lien      : {uri}\n")
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(uri)
            qr.print_ascii(invert=True)
        except ImportError:
            pass
        if not hmac.compare_digest(totp_now(secret), input("Tape le code à 6 chiffres affiché : ").strip()):
            sys.exit("❌ Code incorrect, 2FA non activée. Relance la commande.")
        TOTP_FILE.write_text(secret)
        TOTP_FILE.chmod(0o600)
        print("✅ Double authentification activée.")
        return
    if args.set_password:
        pw = getpass.getpass("Nouveau mot de passe : ")
        if len(pw) < 10 or pw != getpass.getpass("Confirme : "):
            sys.exit("❌ Mots de passe différents ou trop courts (10 caractères min).")
        SECRETS.mkdir(exist_ok=True)
        PASS_FILE.write_text(generate_password_hash(pw))
        PASS_FILE.chmod(0o600)
        print("✅ Mot de passe enregistré.")
        return
    if not PASS_FILE.exists():
        sys.exit("Définis d'abord un mot de passe : python web.py --set-password")
    web_cfg = load_cfg().get("web") or {}
    WEB["_proxies"] = _nets(web_cfg.get("trusted_proxies"))
    WEB["_allowed"] = _nets(web_cfg.get("allowed_ips"))
    if not TOTP_FILE.exists():
        print("⚠️ Double authentification non activée : python web.py --setup-2fa")
    app.secret_key = secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=7 * 86400, MAX_CONTENT_LENGTH=2 * 1024 * 1024)
    from waitress import serve
    print(f"ClipBot web sur http://{args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=8, channel_timeout=30)


if __name__ == "__main__":
    main()
