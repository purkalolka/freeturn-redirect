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
REDIRECT_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Открыть в FreeTurn</title>
<style>body{{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px;text-align:center}}
a.btn{{display:inline-block;margin:24px 0;padding:14px 28px;background:#2b7fff;color:#fff;
border-radius:12px;text-decoration:none;font-size:18px}}
button{{padding:10px 18px;border-radius:10px;border:1px solid #ccc;background:#fff;cursor:pointer}}
code{{word-break:break-all;color:#555}}</style></head><body>
<h1>Открыть в FreeTurn</h1><p>Профиль: <b>{name}</b><br><code>{peer}</code></p>
<a class="btn" href="{link}">Открыть в FreeTurn</a><br>
<button onclick="navigator.clipboard.writeText('{link}')">Скопировать ссылку</button>
<p style="color:#666">Если приложение не открылось само — нажми кнопку выше.
Если FreeTurn не установлен — сначала установи его.</p>
<script>window.location.href = "{link}";</script>
</body></html>"""

LANDING = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FreeTurn Redirect</title></head><body style="font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px">
<h1>FreeTurn Redirect</h1>
<p>Короткие страницы-переходники <code>/r/&lt;code&gt;</code> на <code>freeturn://</code>-ссылки.
Ссылки создаются через Telegram-бота, хранятся только в памяти — после
перезагрузки сервиса пропадают.</p>
<p>Статус: <a href="/health">/health</a></p></body></html>"""

NOT_FOUND = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Ссылка не найдена</title></head><body style="font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 20px">
<h1>Ссылка не найдена</h1><p>Возможно, сервис перезагружался — ссылки живут только в памяти.
Создай её заново через Telegram-бота.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep logs short
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send(json.dumps({"ok": True}), ctype="application/json")
            return
        if path == "/":
            self._send(LANDING)
            return
        if path.startswith("/r/"):
            code = path[len("/r/"):].strip("/")
            with LOCK:
                link = LINKS.get(code)
            if not link:
                self._send(NOT_FOUND, status=404)
                return
            self._send(redirect_page(link))
            return
        if path == "/go":
            q = urllib.parse.parse_qs(parsed.query)
            raw = (q.get("link") or [""])[0]
            obj, err = validate_freeturn(raw)
            if err:
                self._send(f"<h1>Bad link</h1><p>{html.escape(err)}</p>", status=400)
                return
            self._send(redirect_page(raw.strip()))
            return
        self._send(NOT_FOUND, status=404)

    def do_POST(self):  # noqa: N802
        self._send(NOT_FOUND, status=404)


def redirect_page(link):
    obj, _ = validate_freeturn(link)
    name = html.escape(str((obj or {}).get("name") or (obj or {}).get("peer") or ""))
    peer = html.escape(str((obj or {}).get("peer") or ""))
    safe = html.escape(link, quote=True)
    return REDIRECT_PAGE.format(name=name, peer=peer, link=safe)


# ---------------------------------------------------------------- bot
def tg(method, payload):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=35) as r:
        return json.load(r)


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
    threading.Thread(target=bot_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
