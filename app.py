#!/usr/bin/env python3
"""FreeTurn redirector: Telegram bot + redirect site, no database.

One process, two parts:
  * Telegram bot (long polling, raw Bot API, no extra deps) — owner sends a
    freeturn:// link, gets back a short https://<host>/r/<code> page.
  * HTTP site on $PORT (default 8080) — /r/<code> shows a button + JS
    redirect to freeturn://..., /go?link=... is the stateless variant,
    /health is for Northflank.

Storage is an in-memory dict. On every change it is also dumped to
links.json (best effort) so a plain container restart keeps links; a
redeploy / reboot wipes them, which is accepted by design.

Env:
  BOT_TOKEN   Telegram bot token (new bot from @BotFather, NOT @pilolo_robot)
  OWNER_TG_ID Telegram user id allowed to create links (anyone else refused)
  BASE_URL    e.g. https://http--freeturn-redirect--xxxx.code.run (else Host header)
  PORT        default 8080
  DATA_FILE   default links.json
"""
import base64
import html
import json
import os
import random
import string
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = ""  # resolved from BOT_TOKEN via getMe at startup
OWNER_TG_ID = os.environ.get("OWNER_TG_ID", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
DATA_FILE = os.environ.get("DATA_FILE", "links.json")
PACKAGE = "com.freeturn.app"

LINKS = {}  # code -> freeturn:// link
LOCK = threading.Lock()
CODE_ALPHABET = string.ascii_lowercase + string.digits


# ---------------------------------------------------------------- links
def load_links():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            LINKS.update({k: v for k, v in data.items()
                          if isinstance(k, str) and isinstance(v, str)
                          and v.startswith("freeturn://")})
            print(f"loaded {len(LINKS)} links from {DATA_FILE}", flush=True)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 - best effort only
        print(f"load links failed: {e}", flush=True)


def save_links():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(LINKS, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:  # noqa: BLE001 - best effort only
        print(f"save links failed: {e}", flush=True)


def new_code(n=7):
    for _ in range(50):
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(n))
        if code not in LINKS:
            return code
    return "".join(random.choice(CODE_ALPHABET) for _ in range(n + 2))


def validate_freeturn(raw):
    """Return (parsed_dict, error_str). Parsed dict has provider/peer/name/wg."""
    raw = (raw or "").strip()
    if not raw.lower().startswith("freeturn://"):
        return None, "ссылка должна начинаться с freeturn://"
    payload = raw[len("freeturn://"):]
    if not payload:
        return None, "пустой payload после freeturn://"
    if len(raw) > 8192:
        return None, "ссылка слишком длинная (>8 КБ)"
    try:
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode())
        obj = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, "не декодируется: base64url(JSON) битый"
    if not isinstance(obj, dict):
        return None, "payload — не JSON-объект"
    if obj.get("v") != 1:
        return None, f"неподдерживаемая версия v={obj.get('v')!r}, нужна 1"
    if not obj.get("provider"):
        return None, "в JSON нет provider"
    if not obj.get("peer"):
        return None, "в JSON нет peer"
    return obj, None


def describe(parsed):
    name = parsed.get("name") or parsed.get("peer", "?")
    extra = " + WireGuard" if parsed.get("wg") else ""
    return f"{name} ({parsed.get('peer')}){extra}"


def public_base(handler=None):
    if BASE_URL:
        return BASE_URL
    if handler is not None:
        host = handler.headers.get("Host", "localhost:8080")
        return f"https://{host}"
    return "http://localhost:8080"


# ---------------------------------------------------------------- site
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FONT_FILES = {
    "/static/inter-cyrillic.woff2": "inter-cyrillic.woff2",
    "/static/inter-latin.woff2": "inter-latin.woff2",
    "/static/ibm-plex-mono-cyrillic.woff2": "ibm-plex-mono-cyrillic.woff2",
    "/static/ibm-plex-mono-latin.woff2": "ibm-plex-mono-latin.woff2",
    "/static/source-code-pro-cyrillic.woff2": "source-code-pro-cyrillic.woff2",
    "/static/source-code-pro-latin.woff2": "source-code-pro-latin.woff2",
}
FONT_CACHE = {}  # filename -> bytes, read from disk once
OG_IMAGE_FILE = os.path.join(STATIC_DIR, "og.png")
OG_IMAGE_CACHE = None  # bytes, read from disk once

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='16' "
           "fill='%232b7fff'/%3E%3Ctext x='32' y='45' font-family='system-ui,"
           "sans-serif' font-size='36' font-weight='700' fill='%23fff' "
           "text-anchor='middle'%3EF%3C/text%3E%3C/svg%3E")

BASE_CSS = """
@font-face{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;
src:url(/static/inter-cyrillic.woff2) format("woff2");
unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:Inter;font-style:normal;font-weight:100 900;font-display:swap;
src:url(/static/inter-latin.woff2) format("woff2");
unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,
U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,
U+FEFF,U+FFFD}
:root{color-scheme:light dark;--bg:#f7f8fa;--fg:#101828;--muted:#667085;
--card:#fff;--border:#e4e7ec;--accent:#2b7fff;--accent-hover:#1f6fe5;
--accent-fg:#fff;--code-bg:#f2f4f7;
--shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0e13;--fg:#e6e8eb;
--muted:#98a2b3;--card:#141922;--border:#242c38;--accent:#4c8dff;
--accent-hover:#6ba0ff;--accent-fg:#fff;--code-bg:#1b2230;
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35)}}
*{box-sizing:border-box}
html{height:100%}
body{min-height:100%;margin:0;background:var(--bg);color:var(--fg);
font-family:Inter,system-ui,
-apple-system,"Segoe UI",Roboto,sans-serif;font-size:16px;line-height:1.5;
-webkit-font-smoothing:antialiased;display:flex;align-items:center;
justify-content:center;padding:24px}
.wrap{width:100%;max-width:520px}
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;
padding:32px;box-shadow:var(--shadow);text-align:center}
.logo{width:56px;height:56px;border-radius:16px;background:var(--accent);
color:var(--accent-fg);display:inline-flex;align-items:center;
justify-content:center;font-weight:700;font-size:24px;margin-bottom:20px}
h1{font-size:24px;line-height:1.25;margin:0 0 8px;font-weight:650;
letter-spacing:-.02em}
p{margin:0 0 16px;color:var(--muted)}
.profile{font-size:18px;font-weight:600;color:var(--fg);margin:0 0 6px}
.peer{display:inline-block;font-family:ui-monospace,SFMono-Regular,Menlo,
Consolas,monospace;font-size:13px;color:var(--muted);background:var(--code-bg);
border-radius:8px;padding:4px 10px;word-break:break-all;margin-bottom:24px}
.btn{display:flex;align-items:center;justify-content:center;width:100%;
padding:15px 24px;background:var(--accent);color:var(--accent-fg);border:0;
border-radius:12px;font:inherit;font-size:17px;font-weight:600;
text-decoration:none;cursor:pointer;transition:background .15s}
.btn:hover{background:var(--accent-hover)}
.btn.secondary{background:transparent;color:var(--fg);border:1px solid var(--border);
margin-top:10px;font-size:15px;padding:12px 24px}
.btn.secondary:hover{background:var(--code-bg)}
.hint{font-size:13px;color:var(--muted);margin:20px 0 0}
.pill{display:inline-flex;align-items:center;gap:8px;font-size:14px;
font-weight:500;color:var(--fg);background:var(--code-bg);
border:1px solid var(--border);border-radius:999px;padding:6px 14px;
margin-bottom:20px}
.dot{width:8px;height:8px;border-radius:50%;background:#12b76a;
box-shadow:0 0 0 3px rgba(18,183,106,.18)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:.9em;background:var(--code-bg);border-radius:6px;padding:2px 6px;
white-space:nowrap}
.footer{margin-top:20px;font-size:13px;color:var(--muted);text-align:center}
.footer a{color:var(--muted)}
a{color:var(--accent)}
"""


def page(title, body, *, description="", og_title="FreeTurn Redirect",
         og_description="Страница-переходник на freeturn:// ссылки."):
    """Build a full HTML page with the shared head, CSS and neutral OG tags."""
    og_image = f"{BASE_URL}/static/og.png" if BASE_URL else "/static/og.png"
    og_image = html.escape(og_image, quote=True)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f7f8fa" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0e13" media="(prefers-color-scheme: dark)">
<meta name="description" content="{html.escape(description, quote=True)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="FreeTurn Redirect">
<meta property="og:title" content="{html.escape(og_title, quote=True)}">
<meta property="og:description" content="{html.escape(og_description, quote=True)}">
<meta property="og:image" content="{og_image}">
<meta property="og:image:secure_url" content="{og_image}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="FreeTurn Redirect — ASCII donut">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{html.escape(og_title, quote=True)}">
<meta name="twitter:description" content="{html.escape(og_description, quote=True)}">
<meta name="twitter:image" content="{og_image}">
<meta name="twitter:image:alt" content="FreeTurn Redirect — ASCII donut">
<link rel="icon" href="{FAVICON}">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


REDIRECT_BODY = string.Template("""
<div class="card">
  <div class="logo">F</div>
  <h1>Открыть в FreeTurn</h1>
  <p class="profile">$name</p>
  <div class="peer">$peer</div>
  <a class="btn" href="$link" id="open">Открыть в FreeTurn</a>
  <button class="btn secondary" id="copy" type="button">Скопировать ссылку</button>
  <p class="hint">Если приложение не открылось само — нажми кнопку выше.<br>
  Если FreeTurn не установлен — сначала установи его.</p>
</div>
<script>
(function () {
  var link = $link_js;
  var copy = document.getElementById('copy');
  function done() {
    copy.textContent = 'Скопировано';
    setTimeout(function () { copy.textContent = 'Скопировать ссылку'; }, 1600);
  }
  function fallback() {
    var t = document.createElement('textarea');
    t.value = link;
    document.body.appendChild(t);
    t.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    document.body.removeChild(t);
  }
  copy.addEventListener('click', function () {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(done, fallback);
    } else {
      fallback();
    }
  });
  window.location.href = link;
})();
</script>
""")

LANDING_CSS = """
@font-face{font-family:"IBM Plex Mono";font-style:normal;font-weight:700;
font-display:swap;src:url(/static/ibm-plex-mono-cyrillic.woff2) format("woff2");
unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:"IBM Plex Mono";font-style:normal;font-weight:700;
font-display:swap;src:url(/static/ibm-plex-mono-latin.woff2) format("woff2");
unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,
U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,
U+FEFF,U+FFFD}
@font-face{font-family:"Source Code Pro";font-style:normal;font-weight:100 900;
font-display:swap;src:url(/static/source-code-pro-cyrillic.woff2) format("woff2");
unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:"Source Code Pro";font-style:normal;font-weight:100 900;
font-display:swap;src:url(/static/source-code-pro-latin.woff2) format("woff2");
unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,
U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,
U+FEFF,U+FFFD}
:root{--glow:#61b4ff}
body[data-status="down"]{--glow:#ff7f00}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#000;color:#fff;overflow:hidden;display:flex;
align-items:center;justify-content:center;
font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
h1{position:relative;z-index:1;margin:0;padding:0 24px;text-align:center;
font-size:clamp(24px,2.5vw,36px);font-weight:700;line-height:1.2;
letter-spacing:0}
.glow{position:absolute;left:50%;bottom:-47.8vh;width:104.8vw;height:62.6vh;
border-radius:50%;background:var(--glow);filter:blur(15.36vw);opacity:.9;
transform:translateX(-50%);will-change:opacity,transform;pointer-events:none;
animation:glow-opacity 9s ease-in-out infinite,
glow-scale 13s ease-in-out infinite}
@keyframes glow-opacity{
0%,100%{opacity:.82}18%{opacity:1}33%{opacity:.88}
52%{opacity:.98}68%{opacity:.84}84%{opacity:.96}}
@keyframes glow-scale{
0%,100%{transform:translateX(-50%) scale(1)}
25%{transform:translateX(-50%) scale(1.02)}
50%{transform:translateX(-50%) scale(.99)}
75%{transform:translateX(-50%) scale(1.015)}}
@media (prefers-reduced-motion:reduce){.glow{animation:none;opacity:.9}}
.menu{position:fixed;top:26px;left:30px;z-index:2;width:342px;background:#fff;
border-radius:12px;overflow:hidden;color:#000;
font-family:"Source Code Pro",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.menu-head{display:flex;flex-direction:column;gap:6px;padding:24px}
.menu-head h2{margin:0;font-size:18px;font-weight:500;line-height:1.2;color:#000}
.menu-head p{margin:0;font-size:14px;font-weight:400;line-height:1.2;color:#666}
.menu-donut{display:flex;align-items:center;justify-content:center;height:198px;
background:#dedede;overflow:hidden}
.menu-donut pre{margin:0;font:7px/9px "Source Code Pro",ui-monospace,
SFMono-Regular,Menlo,Consolas,monospace;color:#666;white-space:pre;
letter-spacing:0;user-select:none}
.menu-foot{display:flex;align-items:center;justify-content:center;gap:24px;
padding:24px}
.menu-foot a{display:inline-flex;align-items:center;gap:6px;font-size:14px;
font-weight:400;line-height:1.2;color:#666;text-decoration:none}
.menu-foot a:hover{color:#000}
.menu-foot .dot{flex:none;width:16px;height:16px;border-radius:50%;
background:#dedede}
@media (max-width:1199px){.menu{display:none}}
"""

LANDING_JS = """
(function () {
  var body = document.body;
  function check() {
    fetch('/health', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('bad status');
      return r.json();
    }).then(function (j) {
      body.dataset.status = (j && j.ok) ? 'ok' : 'down';
    }).catch(function () {
      body.dataset.status = 'down';
    });
  }
  check();
  setInterval(check, 15000);
})();
(function () {
  var pre = document.getElementById('donut');
  if (!pre) return;
  var COLS = 80, ROWS = 22, CHARS = '.,-~:;=!*#$@';
  var zbuf = new Float32Array(COLS * ROWS);
  var out = new Array(COLS * ROWS);
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var raf = 0, start = 0;
  function render(t) {
    var A = t * 0.0012, B = t * 0.0006;
    var sinA = Math.sin(A), cosA = Math.cos(A);
    var sinB = Math.sin(B), cosB = Math.cos(B);
    for (var k = 0; k < zbuf.length; k++) { zbuf[k] = 0; out[k] = ' '; }
    for (var j = 0; j < 6.283; j += 0.07) {
      var sinJ = Math.sin(j), cosJ = Math.cos(j);
      for (var i = 0; i < 6.283; i += 0.02) {
        var sinI = Math.sin(i), cosI = Math.cos(i);
        var h = cosJ + 2;
        var D = 1 / (sinI * h * sinA + sinJ * cosA + 5);
        var l = cosI, m = cosB, n = sinB;
        var tt = sinI * h * cosA - sinJ * sinA;
        var x = Math.floor(COLS / 2 + 30 * D * (l * h * m - tt * n));
        var y = Math.floor(ROWS / 2 + 1 + 15 * D * (l * h * n + tt * m));
        var o = x + COLS * y;
        var N = Math.floor(8 * ((sinJ * sinA - sinI * cosJ * cosA) * m
          - sinI * cosJ * sinA - sinJ * cosA - l * cosJ * n));
        if (x >= 0 && x < COLS && y >= 0 && y < ROWS && D > zbuf[o]) {
          zbuf[o] = D;
          out[o] = CHARS[N > 0 ? (N < 11 ? N : 11) : 0];
        }
      }
    }
    var s = '';
    for (var r = 0; r < ROWS; r++) {
      s += out.slice(r * COLS, (r + 1) * COLS).join('') + '\\n';
    }
    pre.textContent = s;
  }
  function loop(now) {
    if (pre.offsetParent !== null) {
      if (!start) start = now;
      render(now - start);
    } else {
      start = 0;
    }
    raf = requestAnimationFrame(loop);
  }
  if (reduce) {
    render(0);
  } else {
    raf = requestAnimationFrame(loop);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        cancelAnimationFrame(raf);
        raf = 0;
      } else if (!raf) {
        start = 0;
        raf = requestAnimationFrame(loop);
      }
    });
  }
})();
"""


def landing_page(base_url=""):
    if BOT_USERNAME:
        bot_url = f"https://t.me/{html.escape(BOT_USERNAME, quote=True)}"
        bot_attrs = ' target="_blank" rel="noopener"'
    else:
        bot_url = "#"
        bot_attrs = ' aria-disabled="true"'
    base = base_url.rstrip("/")
    og_image = f"{base}/static/og.png" if base else "/static/og.png"
    og_url = f"{base}/" if base else ""
    og_url_tag = (f'<meta property="og:url" content="{html.escape(og_url, quote=True)}">\n'
                  if og_url else "")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#000000">
<meta name="description" content="FreeTurn Redirect">
<meta property="og:type" content="website">
<meta property="og:site_name" content="FreeTurn Redirect">
<meta property="og:title" content="FreeTurn Redirect">
<meta property="og:description" content="Страница-переходник на freeturn:// ссылки.">
{og_url_tag}<meta property="og:image" content="{html.escape(og_image, quote=True)}">
<meta property="og:image:secure_url" content="{html.escape(og_image, quote=True)}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="FreeTurn Redirect — ASCII donut">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="FreeTurn Redirect">
<meta name="twitter:description" content="Страница-переходник на freeturn:// ссылки.">
<meta name="twitter:image" content="{html.escape(og_image, quote=True)}">
<meta name="twitter:image:alt" content="FreeTurn Redirect — ASCII donut">
<link rel="icon" href="{FAVICON}">
<title>FreeTurn Redirect</title>
<style>{LANDING_CSS}</style></head>
<body data-status="ok">
<div class="glow" aria-hidden="true"></div>
<nav class="menu" aria-label="Quick links">
  <div class="menu-head">
    <h2>menu</h2>
    <p>All quick links for the project are listed here</p>
  </div>
  <div class="menu-donut"><pre id="donut" aria-hidden="true"></pre></div>
  <div class="menu-foot">
    <a href="/health"><span class="dot"></span>/health</a>
    <a href="{bot_url}"{bot_attrs}><span class="dot"></span>/bot</a>
  </div>
</nav>
<h1>FreeTurn Redirect</h1>
<script>{LANDING_JS}</script>
</body></html>"""


NOT_FOUND_BODY = """
<div class="card">
  <div class="logo" style="font-size:18px">404</div>
  <h1>Ссылка не найдена</h1>
  <p>Возможно, сервис перезагружался — ссылки живут только в памяти.
  Создай её заново через Telegram-бота.</p>
  <a class="btn" href="/">На главную</a>
</div>
"""

NOT_FOUND_PAGE = page(
    "Ссылка не найдена", NOT_FOUND_BODY,
    description="Ссылка не найдена или сервис перезапускался.",
).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep logs short
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, body, status=200, ctype="text/html; charset=utf-8",
              cache="no-store"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _send_og(self):
        global OG_IMAGE_CACHE
        if OG_IMAGE_CACHE is None:
            try:
                with open(OG_IMAGE_FILE, "rb") as f:
                    OG_IMAGE_CACHE = f.read()
            except OSError:
                self._send(NOT_FOUND_PAGE, status=404)
                return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(OG_IMAGE_CACHE)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(OG_IMAGE_CACHE)

    def _send_font(self, filename):
        data = FONT_CACHE.get(filename)
        if data is None:
            try:
                with open(os.path.join(STATIC_DIR, filename), "rb") as f:
                    data = f.read()
            except OSError:
                self._send(NOT_FOUND_PAGE, status=404)
                return
            FONT_CACHE[filename] = data
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def do_HEAD(self):  # noqa: N802
        self._head_only = True
        self.do_GET()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send(json.dumps({"ok": True}), ctype="application/json")
            return
        if path == "/":
            self._send(landing_page(public_base(self)))
            return
        if path == "/static/og.png":
            self._send_og()
            return
        if path in FONT_FILES:
            self._send_font(FONT_FILES[path])
            return
        if path.startswith("/r/"):
            code = path[len("/r/"):].strip("/")
            with LOCK:
                link = LINKS.get(code)
            if not link:
                self._send(NOT_FOUND_PAGE, status=404)
                return
            self._send(redirect_page(link))
            return
        if path == "/go":
            q = urllib.parse.parse_qs(parsed.query)
            raw = (q.get("link") or [""])[0]
            obj, err = validate_freeturn(raw)
            if err:
                self._send(bad_link_page(err), status=400)
                return
            self._send(redirect_page(raw.strip()))
            return
        self._send(NOT_FOUND_PAGE, status=404)

    def do_POST(self):  # noqa: N802
        self._send(NOT_FOUND_PAGE, status=404)


def redirect_page(link):
    obj, _ = validate_freeturn(link)
    name = str((obj or {}).get("name") or (obj or {}).get("peer") or "FreeTurn")
    peer = str((obj or {}).get("peer") or "")
    body = REDIRECT_BODY.substitute(
        name=html.escape(name),
        peer=html.escape(peer),
        link=html.escape(link, quote=True),
        link_js=json.dumps(link).replace("</", "<\\/"),
    )
    return page("Открыть в FreeTurn", body,
                description="Открыть профиль в приложении FreeTurn.")


def bad_link_page(err):
    body = ('<div class="card"><div class="logo">!</div>'
            '<h1>Ссылка не подходит</h1>'
            f'<p>{html.escape(err)}</p>'
            '<a class="btn" href="/">На главную</a></div>')
    return page("Ссылка не подходит", body,
                description="Ссылка не подходит для FreeTurn.")


# ---------------------------------------------------------------- bot
def tg(method, payload, timeout=35):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def resolve_bot_username():
    """Ask Telegram who we are, so the landing page can link to t.me/<bot>."""
    global BOT_USERNAME
    if not BOT_TOKEN:
        return
    try:
        me = tg("getMe", {}, timeout=5)
        BOT_USERNAME = (me.get("result") or {}).get("username") or ""
        if BOT_USERNAME:
            print(f"bot username: @{BOT_USERNAME}", flush=True)
    except Exception as e:  # noqa: BLE001 - best effort only
        print(f"getMe failed: {e}", flush=True)


def bot_send(chat_id, text):
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": text,
                           "disable_web_page_preview": True})
    except Exception as e:  # noqa: BLE001
        print(f"sendMessage failed: {e}", flush=True)


def is_owner(user_id):
    if not OWNER_TG_ID:
        return False
    return str(user_id) == OWNER_TG_ID


HELP = ("Пришли мне freeturn:// ссылку — я сделаю короткую страницу:\n"
        "https://<домен>/r/<code>\n\n"
        "Команды:\n/new — прислать ссылку\n/list — живые ссылки\n"
        "/del <code> — удалить\n/delall — удалить все")


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if not is_owner(user_id):
        bot_send(chat_id, "Этот бот только для владельца."
                 + ("" if OWNER_TG_ID else " OWNER_TG_ID не задан."))
        return
    if text in ("/start", "/help", "/new"):
        bot_send(chat_id, HELP)
        return
    if text == "/list":
        with LOCK:
            items = list(LINKS.items())
        base = public_base()
        if not items:
            bot_send(chat_id, "Список пуст — после перезагрузки ссылки пропадают.")
        else:
            lines = [f"{base}/r/{c} — {d}" for c, d in
                     ((c, describe(validate_freeturn(v)[0] or {})) for c, v in items)]
            bot_send(chat_id, "\n".join(lines))
        return
    if text.startswith("/del "):
        code = text.split(None, 1)[1].strip()
        with LOCK:
            gone = LINKS.pop(code, None) is not None
            if gone:
                save_links()
        bot_send(chat_id, f"Удалено: {code}" if gone else f"Нет такого кода: {code}")
        return
    if text == "/delall":
        with LOCK:
            LINKS.clear()
            save_links()
        bot_send(chat_id, "Все ссылки удалены.")
        return
    if text.startswith("/"):
        bot_send(chat_id, "Неизвестная команда. /help")
        return
    # plain text: expect a freeturn:// link (maybe with surrounding text)
    candidate = text
    if "freeturn://" in text.lower():
        i = text.lower().find("freeturn://")
        candidate = text[i:].split()[0]
    obj, err = validate_freeturn(candidate)
    if err:
        bot_send(chat_id, f"Не подходит: {err}\n\nПришли ссылку вида freeturn://eyJ2IjoxLC...")
        return
    code = new_code()
    with LOCK:
        LINKS[code] = candidate
        save_links()
    base = public_base()
    bot_send(chat_id, f"Готово: {describe(obj)}\n{base}/r/{code}\n\n"
                      f"Код: {code} — удалить: /del {code}")


def bot_loop():
    if not BOT_TOKEN:
        print("BOT_TOKEN not set — bot disabled, site only", flush=True)
        return
    offset = 0
    print("bot polling started", flush=True)
    while True:
        try:
            resp = tg("getUpdates", {"offset": offset, "timeout": 30})
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle_message(upd["message"])
        except Exception as e:  # noqa: BLE001
            print(f"poll error: {e}", flush=True)
            time.sleep(5)


def main():
    load_links()
    resolve_bot_username()
    threading.Thread(target=bot_loop, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
