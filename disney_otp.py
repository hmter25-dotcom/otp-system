"""Disney OTP relay: one-time signed links & direct upstream lookup page.

Environment variables
---------------------
ADMIN_PASSWORD            required, admin password for /admin/disney/links
DISNEY_LINK_SECRET        recommended (>= 32 chars). Falls back to a password-derived secret.
DISNEY_DEFAULT_EMAIL      default account email
DISNEY_UPSTREAM_URL       upstream OTP endpoint (https only)
DISNEY_LINK_BASE          public base URL used when generating links
DISNEY_DB_PATH            SQLite file used to persist used links / delivered codes
DISNEY_TRUSTED_PROXY_HOPS number of reverse proxies in front of the app (0 = none)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from flask import Blueprint, Response, jsonify, make_response, request

log = logging.getLogger("elevaraa.disney_otp")
disney_otp_bp = Blueprint("disney_otp", __name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


UPSTREAM_URL = _env("DISNEY_UPSTREAM_URL", "https://anotp.com/loot/fg.php?service=disney")
DEFAULT_EMAIL = _env("DISNEY_DEFAULT_EMAIL", "submind3030@outlook.com").lower()
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
LINK_BASE = _env("DISNEY_LINK_BASE").rstrip("/")
DB_PATH = _env("DISNEY_DB_PATH", "disney_otp.sqlite3")
PROXY_HOPS = max(0, _env_int("DISNEY_TRUSTED_PROXY_HOPS", 0))

if urlparse(UPSTREAM_URL).scheme != "https":
    raise RuntimeError("DISNEY_UPSTREAM_URL must use https")


def _load_secret() -> str:
    explicit = _env("DISNEY_LINK_SECRET")
    if explicit:
        if len(explicit) < 32:
            log.warning("DISNEY_LINK_SECRET is shorter than 32 characters; use a longer one.")
        return explicit
    if not ADMIN_PASSWORD:
        return ""
    log.warning("DISNEY_LINK_SECRET not set; deriving from ADMIN_PASSWORD. Set a dedicated secret.")
    return hashlib.sha256(f"{ADMIN_PASSWORD}|{DEFAULT_EMAIL}|disney_otp".encode()).hexdigest()


LINK_SECRET = _load_secret()

CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
EMAIL_RE = re.compile(r"^[^@\s:]{1,64}@[^@\s:]{1,190}$")
MAX_TOKEN_LEN = 512
CODE_CLAIM_TTL = 3600
EMAIL_MAX_LEN = 254
MAX_BODY_BYTES = 2048
OTP_TTL_SECONDS = 30
UPSTREAM_CACHE_TTL = 2


def _err(message: str, status: int):
    return jsonify({"status": "error", "message": message}), status


def _client_ip() -> str:
    if PROXY_HOPS:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if len(parts) >= PROXY_HOPS:
            return parts[-PROXY_HOPS]
    return request.remote_addr or "unknown"


# --------------------------------------------------------------------------- #
# Rate limiting (in-memory, per process)
# --------------------------------------------------------------------------- #
class Limiter:
    def __init__(self, max_events: int, window: int, max_keys: int = 10_000):
        self.max_events, self.window, self.max_keys = max_events, window, max_keys
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float] | None:
        q = self._events.get(key)
        if q is None:
            return None
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._events[key]
            return None
        return q

    def _sweep(self, now: float) -> None:
        if len(self._events) > self.max_keys:
            for k in list(self._events):
                self._prune(k, now)

    def hit(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._sweep(now)
            q = self._prune(key, now)
            if q is None:
                q = self._events[key] = deque()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            return True

    def is_limited(self, key: str) -> bool:
        with self._lock:
            q = self._prune(key, time.monotonic())
            return q is not None and len(q) >= self.max_events

    def record(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._sweep(now)
            self._events.setdefault(key, deque()).append(now)


ip_limiter = Limiter(120, 300)
token_limiter = Limiter(60, 600)
admin_fail_limiter = Limiter(8, 300)


# --------------------------------------------------------------------------- #
# Tokens & Email Helpers
# --------------------------------------------------------------------------- #
def _sign(payload: str) -> str:
    return hmac.new(LINK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def _valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(EMAIL_RE.match(email))


def _normalize_email(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value or len(value) > EMAIL_MAX_LEN:
        return ""
    return value


def _email_fingerprint(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:10]


def _json(data: dict, status: int = 200):
    resp = make_response(jsonify(data), status)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _fail(message: str, status: int):
    return _json({"status": "error", "message": message}, status)


def make_token(days: int, email: str = "") -> str:
    expiry = int(time.time()) + days * 86400
    clean_email = (email or DEFAULT_EMAIL).strip().lower()
    payload = f"{expiry}:{secrets.token_hex(8)}:{clean_email}"
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def parse_token(token: str) -> tuple[str, str, int] | None:
    if not LINK_SECRET or not token or len(token) > MAX_TOKEN_LEN:
        return None
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        expiry_s, nonce, email = payload.split(":", 2)
        expiry = int(expiry_s)
        if expiry < time.time():
            return None
        return nonce, email, expiry
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# Persistent one-time-use store (SQLite)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path: str):
        self._path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._ready = False

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with self._init_lock:
            if self._ready:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS used_tokens (
                    nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS claimed_codes (
                    code TEXT PRIMARY KEY, nonce TEXT NOT NULL, claimed_at INTEGER NOT NULL);
                """
            )
            self._ready = True

    def is_used(self, nonce: str) -> bool:
        row = self._conn().execute("SELECT 1 FROM used_tokens WHERE nonce = ?", (nonce,)).fetchone()
        return row is not None

    def deliver(self, nonce: str, code: str, token_expiry: int) -> bool:
        conn = self._conn()
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM used_tokens WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM claimed_codes WHERE claimed_at < ?", (now - CODE_CLAIM_TTL,))

            if conn.execute("SELECT 1 FROM used_tokens WHERE nonce = ?", (nonce,)).fetchone():
                conn.execute("ROLLBACK")
                return False
            owner = conn.execute("SELECT nonce FROM claimed_codes WHERE code = ?", (code,)).fetchone()
            if owner is not None and owner[0] != nonce:
                conn.execute("ROLLBACK")
                return False

            conn.execute(
                "INSERT OR REPLACE INTO claimed_codes (code, nonce, claimed_at) VALUES (?, ?, ?)",
                (code, nonce, now),
            )
            conn.execute(
                "INSERT INTO used_tokens (nonce, expires_at) VALUES (?, ?)",
                (nonce, token_expiry + 60),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


store = Store(DB_PATH)


# --------------------------------------------------------------------------- #
# Upstream (with smart short cache + fallback GET/POST + HTML/JSON detection)
# --------------------------------------------------------------------------- #
class UpstreamError(Exception):
    pass


_upstream_cache: dict[str, tuple[float, str | None]] = {}
_upstream_cache_lock = threading.Lock()


def _extract_code_from_payload(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = CODE_RE.search(re.sub(r"<[^>]+>", " ", text))
        return match.group(1) if match else None

    if not isinstance(data, dict):
        return None
    if data.get("success") is False:
        err_msg = str(data.get("error") or "").strip().lower()
        if any(k in err_msg for k in ("not found", "invalid", "unknown", "no account", "غير موجود")):
            return None
        if err_msg and any(k in err_msg for k in ("banned", "blocked", "server", "database")):
            raise UpstreamError(f"upstream error: {err_msg[:200]}")
        return None

    for field in ("otpCode", "code", "otp", "pin"):
        val = data.get(field)
        if val is not None:
            match = CODE_RE.search(str(val))
            if match:
                return match.group(1)
    return None


def _request_upstream(email: str, method: str = "POST") -> str:
    parsed = urlparse(UPSTREAM_URL)
    sep = "&" if parsed.query else "?"
    url = UPSTREAM_URL if method == "POST" else f"{UPSTREAM_URL}{sep}{urlencode({'email': email})}"
    body = urlencode({"email": email}).encode("utf-8") if method == "POST" else None
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0",
        "Referer": UPSTREAM_URL,
    }
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=20) as resp:  # noqa: S310
        return resp.read(65_536).decode("utf-8", errors="replace")


def _fetch_upstream_otp(email: str) -> str | None:
    now = time.monotonic()
    with _upstream_cache_lock:
        cached = _upstream_cache.get(email)
        if cached and (now - cached[0]) < UPSTREAM_CACHE_TTL:
            return cached[1]
        if len(_upstream_cache) > 500:
            _upstream_cache.clear()

    raw = ""
    last_exc: Exception | None = None
    for method in ("POST", "GET"):
        try:
            raw = _request_upstream(email, method=method)
            last_exc = None
            break
        except HTTPError as exc:
            last_exc = exc
            if exc.code in (400, 404):
                return None
        except (URLError, TimeoutError, OSError) as exc:
            last_exc = exc

    if last_exc is not None and not raw:
        raise UpstreamError(f"request failed: {last_exc!r}") from last_exc

    code = _extract_code_from_payload(raw)
    with _upstream_cache_lock:
        _upstream_cache[email] = (time.monotonic(), code)
    return code


# --------------------------------------------------------------------------- #
# API Endpoints
# --------------------------------------------------------------------------- #
@disney_otp_bp.route("/api/disney/code", methods=["POST"])
def api_disney_code():
    if not ip_limiter.hit(_client_ip()):
        return _err("محاولات كثيرة، انتظر قليلاً ثم أعد المحاولة.", 429)

    payload = request.get_json(silent=True)
    token = str((payload if isinstance(payload, dict) else {}).get("token", "")).strip()
    parsed = parse_token(token)
    if not parsed:
        return _err("هذا الرابط غير صالح أو انتهت مدته.", 401)
    nonce, bound_email, expiry = parsed

    try:
        if store.is_used(nonce):
            return _err("تم استلام الكود مسبقاً بهذا الرابط ولا يمكن استخدامه مرة أخرى.", 403)
    except sqlite3.Error:
        log.exception("Store read failed")
        return _err("خطأ مؤقت في الخدمة، حاول بعد قليل.", 503)

    if not token_limiter.hit(nonce):
        return _err("محاولات كثيرة على هذا الرابط، انتظر قليلاً.", 429)

    target_email = bound_email or DEFAULT_EMAIL
    if not _valid_email(target_email):
        return _err("البريد الإلكتروني غير صالح.", 400)

    try:
        code = _fetch_upstream_otp(target_email)
    except UpstreamError:
        log.warning("Upstream OTP fetch failed", exc_info=True)
        return _err("تعذر جلب الرمز حالياً، حاول بعد قليل.", 502)

    if not code:
        return jsonify({"status": "waiting"})

    try:
        delivered = store.deliver(nonce, code, expiry)
    except sqlite3.Error:
        log.exception("Store write failed")
        return _err("خطأ مؤقت في الخدمة، حاول بعد قليل.", 503)

    if not delivered:
        return _err("هذا الكود تم تسليمه مسبقاً أو تم استخدام الرابط.", 403)

    return jsonify({"status": "success", "code": code, "expires_in": 30})


@disney_otp_bp.route("/api/disney/code-by-email", methods=["POST"])
def api_disney_code_by_email():
    ip = _client_ip()
    if not ip_limiter.hit(ip):
        return _fail("محاولات كثيرة، انتظر قليلاً ثم أعد المحاولة.", 429)

    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _fail("الطلب غير صالح.", 413)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _fail("الطلب غير صالح.", 400)

    email = _normalize_email(payload.get("email"))
    if not email or not _valid_email(email):
        return _fail("صيغة البريد الإلكتروني غير صحيحة.", 400)

    if not token_limiter.hit(f"{ip}:{email}") or not token_limiter.hit(f"email:{email}"):
        return _fail("محاولات كثيرة لهذا البريد، انتظر قليلاً.", 429)

    try:
        code = _fetch_upstream_otp(email)
    except UpstreamError:
        log.warning("Disney lookup upstream failed (email=%s)", _email_fingerprint(email), exc_info=True)
        return _fail("تعذر جلب الرمز حالياً، حاول بعد قليل.", 502)
    except Exception:
        log.exception("Disney lookup unexpected error (email=%s)", _email_fingerprint(email))
        return _fail("حدث خطأ غير متوقع.", 500)

    code = str(code).strip() if code else ""
    if not code:
        return _json({"status": "waiting"})

    return _json({"status": "success", "code": code, "expires_in": OTP_TTL_SECONDS})


@disney_otp_bp.route("/admin/disney/links", methods=["POST"])
def admin_disney_links():
    if not ADMIN_PASSWORD or not LINK_SECRET:
        return _err("إعدادات الأدمن غير مكتملة.", 503)

    ip = _client_ip()
    if admin_fail_limiter.is_limited(ip):
        return _err("محاولات كثيرة، حاول لاحقاً.", 429)

    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    supplied = hashlib.sha256(str(data.get("password", "")).encode()).digest()
    expected = hashlib.sha256(ADMIN_PASSWORD.encode()).digest()
    if not hmac.compare_digest(supplied, expected):
        admin_fail_limiter.record(ip)
        return _err("كلمة السر غير صحيحة.", 401)

    try:
        days = max(1, min(int(data.get("days", 30)), 365))
        count = max(1, min(int(data.get("count", 1)), 20))
    except (TypeError, ValueError):
        return _err("قيم غير صالحة.", 400)

    email = str(data.get("email") or DEFAULT_EMAIL).strip().lower()
    if not _valid_email(email):
        return _err("البريد الإلكتروني غير صالح.", 400)

    base = LINK_BASE or request.url_root.rstrip("/")
    links = [f"{base}/disney?t={make_token(days, email)}" for _ in range(count)]
    return jsonify({"status": "success", "links": links, "days": days, "email": email})


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def _page(markup: str) -> Response:
    nonce = secrets.token_urlsafe(16)
    resp = Response(markup.replace("__NONCE__", nonce), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return resp


@disney_otp_bp.route("/disney", methods=["GET"])
def disney_customer_page():
    return _page(CUSTOMER_HTML)


@disney_otp_bp.route("/disney-email", methods=["GET"])
def disney_email_lookup_page():
    return _page(EMAIL_LOOKUP_HTML)


@disney_otp_bp.route("/disney-admin", methods=["GET"])
def disney_admin_page():
    return _page(ADMIN_HTML)


CUSTOMER_HTML = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>طلب كود ديزني</title>
<link href="https://fonts.googleapis.com/css2?family=Changa:wght@700;800&family=Cairo:wght@400;600;700&display=swap" rel="stylesheet">
<style nonce="__NONCE__">
  :root{ --accent:#8d0f0f; --text:#ffffff; --muted:#bdbdbd; }
  *{ box-sizing:border-box; }
  body{
    margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center;
    padding:24px 16px; font-family:'Cairo',sans-serif; color:var(--text);
    background:radial-gradient(circle at 20% 20%, #3a3a3a 0%, #111 35%, #070707 100%);
  }
  .card{
    width:min(460px, 100%);
    background:linear-gradient(180deg, rgba(18,18,18,.94), rgba(8,8,8,.98));
    border:1px solid rgba(255,255,255,.1); border-radius:20px; padding:26px 22px;
    box-shadow:0 18px 60px rgba(0,0,0,.6); text-align:center;
  }
  h1{ font-family:'Changa',sans-serif; margin:0 0 8px; font-size:1.5rem; }
  p.sub{ margin:0 0 20px; color:var(--muted); font-size:.95rem; }
  .btn{
    width:100%; border:0; padding:14px 18px; border-radius:14px;
    background:linear-gradient(90deg, #5c0000 0%, #b30000 55%, #f0dada 100%);
    font-family:'Changa',sans-serif; font-weight:700; font-size:1.1rem; color:#fff; cursor:pointer;
  }
  .btn:disabled{ opacity:.55; cursor:not-allowed; }
  .msg{ margin-top:14px; min-height:1.6em; font-size:.95rem; color:var(--muted); }
  .err{ color:#ff8b8b; }
  .otp-box{
    margin-top:18px; padding:16px; border-radius:16px;
    background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.15);
  }
  .otp-code{ font-size:2.1rem; letter-spacing:5px; font-weight:700; margin:8px 0 12px; direction:ltr; }
  .copy-btn{
    border:1px solid rgba(255,255,255,.4); background:transparent; color:#fff;
    padding:9px 16px; border-radius:12px; cursor:pointer; font-family:'Cairo',sans-serif;
  }
  #circle{
    width:64px; height:64px; border-radius:50%; margin:14px auto 6px;
    background:conic-gradient(#b30000 100%, #e5e5e5 0%);
  }
  @keyframes shake{0%,100%{transform:translateX(0)}20%,60%{transform:translateX(-3px)}40%,80%{transform:translateX(3px)}}
  .shake{ animation:shake .6s infinite; color:#ff8b8b !important; }
  [hidden]{ display:none !important; }
</style>
</head>
<body>
<div class="card">
  <h1>طلب كود ديزني</h1>
  <p class="sub">الرمز يُعرض لمرة واحدة فقط. اطلب الرمز من ديزني ثم اضغط الزر.</p>
  <button id="go" class="btn" type="button">احصل على الرمز</button>
  <div id="msg" class="msg" role="status" aria-live="polite"></div>

  <div id="box" class="otp-box" hidden>
    <div>رمز التحقق</div>
    <div id="code" class="otp-code"></div>
    <button id="copy" class="copy-btn" type="button">نسخ الرمز</button>
    <div id="copied" class="msg" hidden>تم النسخ</div>
    <div id="circle"></div>
    <div class="msg">ينتهي عرض الكود تلقائيًا خلال 30 ثانية.</div>
  </div>
</div>

<script nonce="__NONCE__">
(function(){
  const token = new URLSearchParams(location.search).get('t') || '';
  const go = document.getElementById('go');
  const msg = document.getElementById('msg');
  const box = document.getElementById('box');
  const codeEl = document.getElementById('code');
  const copyBtn = document.getElementById('copy');
  const copied = document.getElementById('copied');
  const circle = document.getElementById('circle');
  let timer = null;

  function setMsg(t, isErr){
    msg.textContent = t;
    msg.className = isErr ? 'msg err' : 'msg';
  }

  function paint(pct){
    circle.style.background = 'conic-gradient(#b30000 ' + pct + '%, #e5e5e5 0%)';
  }

  function startTimer(sec){
    if (timer) clearInterval(timer);
    const total = sec || 30;
    const endAt = Date.now() + total * 1000;
    codeEl.classList.remove('shake');
    paint(100);
    timer = setInterval(() => {
      const left = Math.max(0, Math.ceil((endAt - Date.now()) / 1000));
      paint(Math.max(0, (left / total) * 100));
      if (left <= 4) codeEl.classList.add('shake');
      if (left <= 0){
        clearInterval(timer);
        timer = null;
        codeEl.textContent = '';
        box.hidden = true;
        setMsg('انتهى وقت عرض الكود.', false);
      }
    }, 500);
  }

  go.addEventListener('click', async () => {
    if (!token){ setMsg('الرابط غير صالح.', true); return; }
    go.disabled = true;
    box.hidden = true;
    setMsg('جاري جلب الرمز…', false);
    try {
      const res = await fetch('/api/disney/code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token })
      });
      let data = {};
      try { data = await res.json(); } catch (_) {}

      if (res.ok && data.status === 'waiting'){
        setMsg('لم يصل الرمز بعد. اطلبه من ديزني ثم حاول مجدداً.', false);
        go.disabled = false;
        return;
      }
      if (!res.ok || data.status !== 'success'){
        setMsg(data.message || 'تعذر جلب الرمز.', true);
        if (res.status !== 403 && res.status !== 401) go.disabled = false;
        return;
      }
      codeEl.textContent = data.code;
      box.hidden = false;
      setMsg('', false);
      startTimer(data.expires_in || 30);
    } catch (e){
      setMsg('تعذر الاتصال بالسيرفر.', true);
      go.disabled = false;
    }
  });

  copyBtn.addEventListener('click', async () => {
    const val = codeEl.textContent.trim();
    if (!val) return;
    try { await navigator.clipboard.writeText(val); } catch (e) {}
    copied.hidden = false;
    setTimeout(() => { copied.hidden = true; }, 1600);
  });

  if (!token){
    go.disabled = true;
    setMsg('الرابط غير صالح. تواصل مع البائع.', true);
  }
})();
</script>
</body>
</html>
"""

EMAIL_LOOKUP_HTML = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark">
<title>طلب كود ديزني</title>
<link href="https://fonts.googleapis.com/css2?family=Changa:wght@700;800&family=Cairo:wght@400;600;700&display=swap" rel="stylesheet">
<style nonce="__NONCE__">
  :root{ --text:#ffffff; --muted:#bdbdbd; }
  *{ box-sizing:border-box; }
  body{
    margin:0; min-height:100vh; min-height:100dvh; display:grid; place-items:center;
    padding:24px 16px; font-family:'Cairo',sans-serif; color:var(--text);
    background:radial-gradient(circle at 20% 20%, #3a3a3a 0%, #111 35%, #070707 100%);
  }
  .card{
    width:min(460px, 100%);
    background:linear-gradient(180deg, rgba(18,18,18,.94), rgba(8,8,8,.98));
    border:1px solid rgba(255,255,255,.1); border-radius:20px; padding:26px 22px;
    box-shadow:0 18px 60px rgba(0,0,0,.6); text-align:center;
  }
  h1{ font-family:'Changa',sans-serif; margin:0 0 8px; font-size:1.55rem; }
  p.sub{ margin:0 0 22px; color:var(--muted); font-size:.95rem; line-height:1.6; }
  label{ display:block; text-align:right; margin-bottom:8px; font-weight:600; font-size:.95rem; }
  .inp{
    width:100%; padding:13px 14px; border-radius:12px; margin-bottom:16px;
    border:1px solid rgba(255,255,255,.16); background:#0c0c0c; color:#fff;
    font-family:'Cairo',sans-serif; font-size:1rem; direction:ltr; text-align:right;
  }
  .inp:focus{ outline:none; border-color:#c81e1e; }
  .btn{
    width:100%; border:0; padding:14px 18px; border-radius:14px;
    background:linear-gradient(90deg, #5c0000 0%, #b30000 55%, #f0dada 100%);
    font-family:'Changa',sans-serif; font-weight:700; font-size:1.1rem; color:#fff; cursor:pointer;
  }
  .btn:disabled{ opacity:.55; cursor:not-allowed; }
  .msg{ margin-top:14px; min-height:1.6em; font-size:.95rem; color:var(--muted); }
  .err{ color:#ff8b8b; }
  .otp-box{
    margin-top:18px; padding:18px 16px; border-radius:16px;
    background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.12);
  }
  .otp-title{ color:var(--muted); font-size:.9rem; }
  .otp-code{ font-size:2.1rem; letter-spacing:6px; font-weight:800; margin:10px 0 14px; direction:ltr; }
  .copy-btn{
    border:1px solid rgba(255,255,255,.35); background:rgba(255,255,255,.06); color:#fff;
    padding:8px 16px; border-radius:10px; cursor:pointer; font-family:'Cairo',sans-serif; font-size:.9rem;
  }
  #circle{
    width:56px; height:56px; border-radius:50%; margin:16px auto 8px;
    background:conic-gradient(#c81e1e 100%, #333 0);
  }
  @keyframes shake{0%,100%{transform:translateX(0)}20%,60%{transform:translateX(-3px)}40%,80%{transform:translateX(3px)}}
  .shake{ animation:shake .6s infinite; color:#ff8b8b !important; }
  @media (prefers-reduced-motion: reduce){ .shake{ animation:none; } }
  [hidden]{ display:none !important; }
</style>
</head>
<body>
<div class="card">
  <h1>طلب كود ديزني</h1>
  <p class="sub">أدخل بريدك المرتبط في النظام وسيتم جلب رمز التحقق الخاص بخدمة +Disney.</p>

  <label for="email">البريد الإلكتروني</label>
  <input id="email" class="inp" type="email" inputmode="email" autocomplete="email"
         autocapitalize="off" spellcheck="false" maxlength="254" placeholder="example@domain.com">

  <button id="go" class="btn" type="button">احصل على الرمز</button>
  <div id="msg" class="msg" role="status" aria-live="polite"></div>

  <div id="box" class="otp-box" hidden>
    <div class="otp-title">رمز التحقق</div>
    <div id="code" class="otp-code" dir="ltr"></div>
    <button id="copy" class="copy-btn" type="button">نسخ الرمز</button>
    <div id="copied" class="msg" role="status" hidden></div>
    <div id="circle" aria-hidden="true"></div>
    <div class="msg">ينتهي عرض الكود تلقائيًا خلال 30 ثانية.</div>
  </div>
</div>

<script nonce="__NONCE__">
(function(){
  'use strict';

  const REQUEST_TIMEOUT_MS = 15000;
  const RETRY_COOLDOWN_MS  = 2500;
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  const emailInp = document.getElementById('email');
  const go       = document.getElementById('go');
  const msg      = document.getElementById('msg');
  const box      = document.getElementById('box');
  const codeEl   = document.getElementById('code');
  const copyBtn  = document.getElementById('copy');
  const copied   = document.getElementById('copied');
  const circle   = document.getElementById('circle');

  let timer = null;
  let copiedTimer = null;
  let busy = false;

  function setMsg(text, isErr){
    msg.textContent = text;
    msg.className = isErr ? 'msg err' : 'msg';
  }

  function paint(pct){
    circle.style.background = 'conic-gradient(#c81e1e ' + pct + '%, #333 0)';
  }

  function stopTimer(){
    if (timer){ clearInterval(timer); timer = null; }
  }

  function hideResult(){
    stopTimer();
    codeEl.textContent = '';
    codeEl.classList.remove('shake');
    copied.hidden = true;
    box.hidden = true;
  }

  function startTimer(sec){
    stopTimer();
    const total = Number(sec) > 0 ? Number(sec) : 30;
    const endAt = Date.now() + total * 1000;
    codeEl.classList.remove('shake');
    paint(100);
    timer = setInterval(() => {
      const left = Math.max(0, Math.ceil((endAt - Date.now()) / 1000));
      paint((left / total) * 100);
      if (left <= 4) codeEl.classList.add('shake');
      if (left <= 0){
        hideResult();
        setMsg('انتهى وقت عرض الكود.', false);
      }
    }, 500);
  }

  function setBusy(v){
    busy = v;
    go.disabled = v;
  }

  async function requestCode(){
    if (busy) return;

    const email = emailInp.value.trim().toLowerCase();
    if (!email){ setMsg('أدخل البريد الإلكتروني أولاً.', true); return; }
    if (!EMAIL_RE.test(email)){ setMsg('صيغة البريد غير صحيحة.', true); return; }

    setBusy(true);
    hideResult();
    setMsg('جاري جلب الرمز…', false);

    const ctrl = new AbortController();
    const abortTimer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);

    try {
      const res = await fetch('/api/disney/code-by-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
        cache: 'no-store',
        credentials: 'same-origin',
        signal: ctrl.signal
      });

      let data = {};
      try { data = await res.json(); } catch (_) {}

      if (res.ok && data.status === 'waiting'){
        setMsg('لم يصل الرمز بعد. اطلب الرمز من ديزني ثم حاول مجدداً.', false);
        return;
      }
      if (!res.ok || data.status !== 'success' || !data.code){
        setMsg(data.message || 'تعذر جلب الرمز.', true);
        return;
      }

      codeEl.textContent = String(data.code);
      box.hidden = false;
      setMsg('', false);
      startTimer(data.expires_in);
    } catch (e){
      setMsg(e && e.name === 'AbortError' ? 'انتهت مهلة الطلب، حاول مجدداً.' : 'تعذر الاتصال بالسيرفر.', true);
    } finally {
      clearTimeout(abortTimer);
      setTimeout(() => setBusy(false), RETRY_COOLDOWN_MS);
    }
  }

  go.addEventListener('click', requestCode);
  emailInp.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter'){ ev.preventDefault(); requestCode(); }
  });

  copyBtn.addEventListener('click', async () => {
    const val = codeEl.textContent.trim();
    if (!val) return;
    let ok = false;
    try {
      await navigator.clipboard.writeText(val);
      ok = true;
    } catch (_) {}
    copied.textContent = ok ? 'تم النسخ' : 'تعذر النسخ، انسخ الرمز يدويًا.';
    copied.className = ok ? 'msg' : 'msg err';
    copied.hidden = false;
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => { copied.hidden = true; }, 1600);
  });
})();
</script>
</body>
</html>
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>إنشاء روابط عملاء ديزني</title>
<style nonce="__NONCE__">
  *{ box-sizing:border-box; }
  body{ margin:0; padding:24px 16px; font-family:system-ui, sans-serif; background:#0c0c0c; color:#fff; }
  main{ max-width:500px; margin:0 auto; }
  label{ display:block; margin:12px 0 6px; color:#bbb; }
  input, select{
    width:100%; padding:11px 13px; border-radius:10px;
    border:1px solid #333; background:#161616; color:#fff; font:inherit;
  }
  .row{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  button{
    margin-top:16px; width:100%; padding:12px; border:0; border-radius:10px;
    background:#b30000; color:#fff; font-weight:700; cursor:pointer;
  }
  button:disabled{ opacity:.55; cursor:not-allowed; }
  button.secondary{ background:#2b2b2b; }
  #msg{ margin-top:10px; min-height:1.4em; }
  ul{ list-style:none; padding:0; margin:16px 0 0; display:grid; gap:10px; }
  li{ background:#161616; border:1px solid #2b2b2b; border-radius:10px; padding:10px; word-break:break-all; }
  [hidden]{ display:none !important; }
</style>
</head>
<body>
<main>
  <h2>إنشاء روابط عملاء ديزني</h2>
  <label for="pw">كلمة سر الأدمن</label>
  <input id="pw" type="password" autocomplete="current-password">
  <label for="email">بريد الحساب (اختياري)</label>
  <input id="email" type="email" placeholder="submind3030@outlook.com">
  <div class="row">
    <div>
      <label for="days">المدة (أيام)</label>
      <select id="days">
        <option value="1">1</option>
        <option value="7">7</option>
        <option value="30" selected>30</option>
        <option value="90">90</option>
      </select>
    </div>
    <div>
      <label for="count">العدد</label>
      <input id="count" type="number" min="1" max="20" value="1">
    </div>
  </div>
  <button id="go" type="button">إنشاء الروابط</button>
  <button id="copyAll" class="secondary" type="button" hidden>نسخ كل الروابط</button>
  <div id="msg" role="status" aria-live="polite"></div>
  <ul id="list"></ul>
</main>
<script nonce="__NONCE__">
(function(){
  const $ = id => document.getElementById(id);
  const go = $('go'), copyAll = $('copyAll'), msg = $('msg'), list = $('list');
  let links = [];

  go.addEventListener('click', async () => {
    go.disabled = true;
    list.replaceChildren();
    copyAll.hidden = true;
    msg.textContent = 'جاري الإنشاء…';
    try {
      const res = await fetch('/admin/disney/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          password: $('pw').value,
          email: $('email').value.trim(),
          days: Number($('days').value),
          count: Number($('count').value)
        })
      });
      let data = {};
      try { data = await res.json(); } catch (_) {}
      if (!res.ok || data.status !== 'success'){ msg.textContent = data.message || 'خطأ'; return; }

      links = data.links;
      msg.textContent = 'تم الإنشاء (' + data.email + ')';
      links.forEach(u => {
        const li = document.createElement('li');
        li.textContent = u;
        list.appendChild(li);
      });
      copyAll.hidden = false;
    } catch (e){
      msg.textContent = 'تعذر الاتصال بالسيرفر.';
    } finally {
      go.disabled = false;
    }
  });

  copyAll.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(links.join('\n'));
      msg.textContent = 'تم نسخ الروابط.';
    } catch (e){
      msg.textContent = 'تعذر النسخ، انسخها يدوياً.';
    }
  });
})();
</script>
</body>
</html>
"""
