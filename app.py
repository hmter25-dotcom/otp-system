"""Elevaraa Core Unified Server (refactored)."""
from __future__ import annotations

import datetime
import hmac
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import uuid
import pyotp
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from functools import wraps
from imaplib import IMAP4_SSL
from typing import Any, Callable, Iterator

import requests
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# Logging
# ==========================================
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("elevaraa")

# ==========================================
# Configuration
# ==========================================
@dataclass(frozen=True)
class Settings:
    admin_password: str
    email_account: str
    email_password: str
    disney_email_account: str
    disney_email_password: str
    youtube_secret: str
    youtube_email: str
    youtube_pass: str

    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    imap_timeout: int = 15
    blocklist_file: str = "blocked_subs.json"
    mapping_file: str = "sub_mappings.json"
    youtube_db: str = "youtube_links.db"
    netflix_api: str = "https://tv.ostories.me/api/dealer-subscriptions"
    netflix_timeout: int = 4
    otp_link_base: str = "https://elevara1.shop/otp.netflix/?subscriptionId="

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        s = cls(
            admin_password=env("ADMIN_PASSWORD", ""),
            email_account=env("EMAIL_ACCOUNT", ""),
            email_password=env("EMAIL_PASSWORD", ""),
            disney_email_account=env("DISNEY_EMAIL_ACCOUNT", ""),
            disney_email_password=env("DISNEY_EMAIL_PASSWORD", ""),
            youtube_secret=env("YOUTUBE_SECRET", ""),
            youtube_email=env("YOUTUBE_EMAIL", ""),
            youtube_pass=env("YOUTUBE_PASS", ""),
        )
        s._warn_missing()
        return s

    def _warn_missing(self) -> None:
        required = {
            "ADMIN_PASSWORD": self.admin_password,
            "EMAIL_ACCOUNT": self.email_account,
            "EMAIL_PASSWORD": self.email_password,
            "DISNEY_EMAIL_ACCOUNT": self.disney_email_account,
            "DISNEY_EMAIL_PASSWORD": self.disney_email_password,
            "YOUTUBE_SECRET": self.youtube_secret,
        }
        for name, value in required.items():
            if not value:
                log.warning("Env var %s is not set; related feature disabled", name)

settings = Settings.from_env()
Config = settings

SUB_ID_PATTERN = re.compile(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", re.I)
URL_PATTERN = re.compile(r"(https?://[^\s]+)")

app = Flask(__name__)
CORS(app)

try:
    from canva import canva_bp
    app.register_blueprint(canva_bp)
except ImportError:
    log.info("Canva module not found; skipping")
except Exception:
    log.exception("Canva module failed to load")

# ==========================================
# Responses & Auth
# ==========================================
def error(message: str, status: int):
    return jsonify({"status": "error", "message": message}), status

def admin_required(unauthorized_message: str) -> Callable:
    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if not settings.admin_password:
                return error("Admin access is not configured", 503)
            data = request.get_json(silent=True) or {}
            supplied = str(data.get("password", "")).encode()
            if not hmac.compare_digest(supplied, settings.admin_password.encode()):
                return error(unauthorized_message, 401)
            return view(data, *args, **kwargs)
        return wrapper
    return decorator

# ==========================================
# JSON store
# ==========================================
class JsonStore:
    def __init__(self, path: str, default: Any):
        self.path = path
        self.default = default
        self._lock = threading.RLock()

    def load(self) -> Any:
        with self._lock:
            if not os.path.exists(self.path): return self._fresh_default()
            try:
                with open(self.path, "r", encoding="utf-8") as f: return json.load(f)
            except (OSError, json.JSONDecodeError): return self._fresh_default()

    def save(self, data: Any) -> bool:
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            tmp_name = None
            try:
                with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
                    tmp_name = tmp.name
                    json.dump(data, tmp, ensure_ascii=False)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self.path)
                return True
            except OSError:
                if tmp_name and os.path.exists(tmp_name): os.remove(tmp_name)
                return False

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._lock:
            data = self.load()
            yield data
            self.save(data)

    def _fresh_default(self) -> Any:
        return json.loads(json.dumps(self.default))

blocked_store = JsonStore(settings.blocklist_file, [])
mappings_store = JsonStore(settings.mapping_file, {})

def load_blocked() -> list: return blocked_store.load()
def load_mappings() -> dict: return mappings_store.load()
def blocked_id(entry: Any) -> Any: return entry.get("id") if isinstance(entry, dict) else entry
def clean_sub_id(raw: str | None) -> str:
    if not raw: return ""
    match = SUB_ID_PATTERN.search(raw)
    return match.group(1).lower() if match else raw.strip().lower()

# ==========================================
# YouTube DB
# ==========================================
@contextmanager
def yt_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.youtube_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_yt_db() -> None:
    try:
        with yt_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS links (token TEXT PRIMARY KEY, used INTEGER DEFAULT 0)")
    except sqlite3.Error: pass

def claim_yt_token(token: str) -> bool:
    with yt_db() as conn:
        cur = conn.execute("UPDATE links SET used = 1 WHERE token = ? AND used = 0", (token,))
        return cur.rowcount == 1

init_yt_db()

# ==========================================
# Helpers (IMAP / HTTP)
# ==========================================
@contextmanager
def imap_session(account: str, password: str) -> Iterator[IMAP4_SSL]:
    if not account or not password: raise ValueError("IMAP credentials missing")
    mail = IMAP4_SSL(settings.imap_server, settings.imap_port, timeout=settings.imap_timeout)
    try:
        mail.login(account, password)
        yield mail
    finally:
        try: mail.logout()
        except Exception: pass

def decode_subject(raw_subject: str | None) -> str:
    parts = []
    for text, charset in decode_header(raw_subject or ""):
        if isinstance(text, bytes): parts.append(text.decode(charset or "utf-8", errors="ignore"))
        else: parts.append(str(text))
    return "".join(parts)

def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3, status_forcelist=(502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session

http = build_http_session()

# ==========================================
# OSN
# ==========================================
def get_genius_otp():
    try:
        with imap_session(settings.email_account, settings.email_password) as mail:
            mail.select("INBOX")
            status, messages = mail.search(None, "ALL")
            if status != "OK" or not messages[0]: return None, None
            for e_id in reversed(messages[0].split()[-15:]):
                _, msg_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")
                for part in msg_data:
                    if not isinstance(part, tuple): continue
                    msg = message_from_bytes(part[1])
                    if "osn" not in str(msg.get("From", "")).lower(): continue
                    match = re.search(r"\b(\d{4})\b", decode_subject(msg.get("Subject")))
                    if match: return match.group(1), 4
    except Exception: pass
    return None, None

@app.route("/get-otp", methods=["GET"])
def get_otp():
    code, length = get_genius_otp()
    if code: return jsonify({"status": "success", "otp": code, "length": length})
    return jsonify({"status": "waiting", "otp": None, "length": 0})

# ==========================================
# Netflix
# ==========================================
NETFLIX_ENDPOINTS = (
    ("signin_code", "signin-code", ("code", "signInCode"), ("email", "accountEmail")),
    ("temp_code", "temp-code", ("code", "tempCode"), ("email",)),
    ("login_code", "login-verification-code", ("code", "loginVerificationCode", "url"), ("email",)),
)

def _first_value(data, keys):
    for key in keys:
        if value := data.get(key): return value
    return None

@app.route("/get-netflix", methods=["GET"])
def get_netflix():
    sub_id = clean_sub_id(request.args.get("sub_id"))
    if not sub_id: return error("ID required", 400)
    if sub_id in {blocked_id(b) for b in load_blocked()}: return jsonify({"status": "blocked", "message": "نعتذر، انتهت صلاحية هذا الرابط."}), 403

    backend_key = load_mappings().get(sub_id, sub_id)
    headers = {"accept": "application/json", "referer": f"https://tv.ostories.me/?subscriptionId={backend_key}", "user-agent": "Mozilla/5.0", "x-api-key": backend_key}
    result = {"status": "success", "email": None, "signin_code": None, "temp_code": None, "login_code": None, "approval_url": None}

    for key, path, code_fields, email_fields in NETFLIX_ENDPOINTS:
        try:
            resp = http.get(f"{settings.netflix_api}/{path}", headers=headers, timeout=settings.netflix_timeout)
            if resp.status_code == 200:
                data = resp.json()
                result[key] = _first_value(data, code_fields)
                if not result["email"]: result["email"] = _first_value(data, email_fields)
        except Exception: pass

    for key in ("signin_code", "temp_code", "login_code"):
        if match := URL_PATTERN.search(str(result.get(key) or "")): result["approval_url"] = match.group(1)

    return jsonify(result)

# ==========================================
# Disney+
# ==========================================
@app.route("/get-disney", methods=["POST"])
def fetch_disney_code():
    data = request.get_json(silent=True) or {}
    target_email = data.get("email", "").strip().lower()
    if not target_email or "elevaraa11+" not in target_email: return error("إيميل غير صالح.", 400)

    try:
        with imap_session(settings.disney_email_account, settings.disney_email_password) as mail:
            mail.select("INBOX")
            status, messages = mail.search(None, f'(TO "{target_email}" FROM "MyDisney")')
            if status != "OK" or not messages[0]: return jsonify({"status": "error", "message": "لم يتم العثور على رسالة."})

            latest_id = messages[0].split()[-1]
            _, msg_data = mail.fetch(latest_id, "(RFC822)")
            for part in msg_data:
                if not isinstance(part, tuple): continue
                msg = message_from_bytes(part[1])
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore') if not msg.is_multipart() else "".join(
                    sub.get_payload(decode=True).decode('utf-8', errors='ignore') for sub in msg.walk() if sub.get_content_type() in ["text/plain", "text/html"]
                )
                if match := re.search(r'\b\d{6}\b', body):
                    mail.store(latest_id, '+FLAGS', '\\Deleted')
                    mail.expunge()
                    return jsonify({"status": "success", "code": match.group(0)})
            return jsonify({"status": "error", "message": "لم يتم التعرف على الكود."})
    except Exception: return error("تعذر الاتصال بالبريد.", 500)

# ==========================================
# YouTube Premium (OTP Links)
# ==========================================
@app.route('/yt/<token>', methods=['GET'])
def youtube_magic_link(token):
    if not claim_yt_token(token):
        return "<h2 style='color:red; text-align:center; margin-top:50px;'>🚫 عذراً، الرابط غير صحيح أو تم استخدامه مسبقاً.</h2>", 403
    
    totp = pyotp.TOTP(settings.youtube_secret)
    current_code = totp.now()
    
    success_html = f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>بيانات الدخول | Premium</title>
        <script src="https://cdn.tailwindcss.com"></script><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
        <style>body {{ background-color: #0f0f0f; color: #fff; font-family: 'Segoe UI', sans-serif; }} .glass {{ background: rgba(30, 30, 30, 0.8); backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1); }} .progress-bar {{ transition: width 1s linear; }}</style>
    </head>
    <body class="flex items-center justify-center min-h-screen p-4">
        <div class="glass p-8 rounded-2xl shadow-2xl w-full max-w-md relative overflow-hidden">
            <div class="text-center mb-8"><i class="fab fa-youtube text-red-600 text-5xl mb-3 shadow-lg"></i><h2 class="text-2xl font-bold text-white tracking-wide">تصريح دخول بريميوم</h2></div>
            <div class="space-y-5">
                <div class="bg-black bg-opacity-40 p-4 rounded-xl flex justify-between items-center border border-gray-800">
                    <div><p class="text-xs text-gray-500 mb-1">البريد الإلكتروني</p><p class="font-mono text-sm" id="emailTxt">{settings.youtube_email}</p></div>
                    <button onclick="copyData('emailTxt', this)" class="bg-gray-800 text-white p-2 rounded-lg"><i class="far fa-copy"></i></button>
                </div>
                <div class="bg-black bg-opacity-40 p-4 rounded-xl flex justify-between items-center border border-gray-800">
                    <div><p class="text-xs text-gray-500 mb-1">كلمة المرور</p><p class="font-mono text-sm" id="passTxt">{settings.youtube_pass}</p></div>
                    <button onclick="copyData('passTxt', this)" class="bg-gray-800 text-white p-2 rounded-lg"><i class="far fa-copy"></i></button>
                </div>
                <div class="bg-red-600 bg-opacity-10 p-5 rounded-xl border border-red-900 text-center">
                    <p class="text-xs text-red-400 mb-2 font-bold"><i class="fas fa-shield-alt ml-1"></i>كود التحقق الآمن (2FA)</p>
                    <h1 class="text-4xl font-mono tracking-[0.25em] text-white my-3" id="otpTxt">{current_code}</h1>
                    <div class="w-full bg-gray-900 h-1 mt-5 rounded-full overflow-hidden"><div id="timeBar" class="bg-red-500 h-full progress-bar" style="width: 100%"></div></div>
                    <p class="text-[10px] text-gray-500 mt-2" id="timeText">صالح لمدة 30 ثانية...</p>
                </div>
            </div>
        </div>
        <script>
            function copyData(id, btn) {{ navigator.clipboard.writeText(document.getElementById(id).innerText); btn.innerHTML = '<i class="fas fa-check text-green-400"></i>'; setTimeout(() => btn.innerHTML = '<i class="far fa-copy"></i>', 2000); }}
            function updateTimer() {{
                const timeLeft = 30 - (Math.floor(Date.now() / 1000) % 30);
                document.getElementById('timeBar').style.width = (timeLeft / 30 * 100) + '%';
                if(timeLeft <= 5) document.getElementById('timeBar').classList.replace('bg-red-500', 'bg-orange-500');
                else document.getElementById('timeBar').classList.replace('bg-orange-500', 'bg-red-500');
                if(timeLeft === 30) document.getElementById('otpTxt').innerHTML = "-------";
            }}
            setInterval(updateTimer, 1000); updateTimer();
        </script>
    </body>
    </html>
    """
    return render_template_string(success_html)

# ==========================================
# Admin Dashboard
# ==========================================
ADMIN_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8"><title>لوحة تحكم الروابط | Elevaraa</title>
    <script src="https://cdn.tailwindcss.com"></script><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8fafc; } .glass-panel { background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px); }</style>
</head>
<body class="text-gray-800">
    <div id="loginScreen" class="flex h-screen items-center justify-center">
        <div class="glass-panel p-8 rounded-xl shadow-lg w-96 text-center border border-gray-100">
            <h2 class="text-2xl font-bold text-gray-800 mb-6"><i class="fas fa-shield-alt text-red-600 ml-2"></i>الإدارة المركزية</h2>
            <input type="password" id="adminPassword" placeholder="رمز الدخول السري" class="w-full p-3 border rounded-lg mb-4 text-center focus:ring-2 focus:ring-red-500 outline-none">
            <button onclick="login()" class="w-full bg-red-600 text-white font-bold py-3 rounded-lg hover:bg-red-700 transition">تسجيل الدخول</button>
            <p id="loginError" class="text-red-500 text-sm mt-3 hidden">رمز المرور غير صحيح!</p>
        </div>
    </div>
    <div id="dashboardScreen" class="hidden min-h-screen">
        <nav class="bg-gray-900 text-white p-4 shadow-md">
            <div class="container mx-auto flex justify-between items-center"><h1 class="text-xl font-bold"><i class="fab fa-youtube text-red-500 ml-2"></i>موزع يوتيوب بريميوم</h1><button onclick="logout()" class="text-sm bg-gray-700 px-4 py-2 rounded">تسجيل الخروج</button></div>
        </nav>
        <div class="container mx-auto mt-8 p-4 grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="glass-panel p-6 rounded-xl shadow border border-gray-100 h-fit">
                <h3 class="text-lg font-bold mb-4 border-b pb-2">توليد روابط جديدة</h3>
                <label class="block text-sm text-gray-600 mb-2">العدد المطلوب (مثال: 5):</label>
                <input type="number" id="linkCount" value="1" min="1" max="50" class="w-full p-3 border rounded-lg mb-4 outline-none">
                <button onclick="generateLinks()" class="w-full bg-red-600 text-white font-bold py-3 rounded-lg flex justify-center items-center"><i class="fas fa-magic ml-2"></i> توليد الروابط</button>
            </div>
            <div class="glass-panel p-6 rounded-xl shadow border border-gray-100 md:col-span-2">
                <div class="flex justify-between items-center mb-4 border-b pb-2"><h3 class="text-lg font-bold">الروابط المُصدرة</h3><button onclick="fetchLinks()" class="text-blue-600"><i class="fas fa-sync-alt"></i> تحديث</button></div>
                <div class="overflow-x-auto">
                    <table class="w-full text-right">
                        <thead><tr class="bg-gray-50 text-gray-600 text-sm"><th class="p-3">الحالة</th><th class="p-3">الرابط</th><th class="p-3 text-center">إجراء</th></tr></thead>
                        <tbody id="linksTableBody"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    <script>
        let adminPass = '';
        function login() { adminPass = document.getElementById('adminPassword').value; fetchLinks(); }
        function logout() { adminPass = ''; document.getElementById('dashboardScreen').classList.add('hidden'); document.getElementById('loginScreen').classList.remove('hidden'); document.getElementById('adminPassword').value = ''; }
        async function fetchLinks() {
            const res = await fetch('/admin/youtube/list', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: adminPass}) });
            if (res.status === 401) { document.getElementById('loginError').classList.remove('hidden'); return; }
            const data = await res.json();
            document.getElementById('loginError').classList.add('hidden'); document.getElementById('loginScreen').classList.add('hidden'); document.getElementById('dashboardScreen').classList.remove('hidden');
            const tbody = document.getElementById('linksTableBody'); tbody.innerHTML = '';
            data.links.forEach(link => {
                const statusBadge = link.used ? `<span class="bg-red-100 text-red-700 px-2 py-1 rounded text-xs font-bold">مُستخدم</span>` : `<span class="bg-green-100 text-green-700 px-2 py-1 rounded text-xs font-bold">نشط</span>`;
                tbody.innerHTML += `<tr class="border-b last:border-0 hover:bg-gray-50"><td class="p-3">${statusBadge}</td><td class="p-3 text-sm font-mono text-gray-600" dir="ltr">${link.url}</td><td class="p-3 text-center"><button onclick="navigator.clipboard.writeText('${link.url}'); alert('تم النسخ!');" class="text-blue-500 p-2"><i class="far fa-copy"></i></button></td></tr>`;
            });
        }
        async function generateLinks() {
            const count = document.getElementById('linkCount').value;
            const res = await fetch('/admin/youtube/generate_bulk', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: adminPass, count: count}) });
            if (res.ok) fetchLinks(); else alert("خطأ!");
        }
    </script>
</body>
</html>
"""

@app.route("/admin/dashboard", methods=["GET"])
def admin_dashboard():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/youtube/list", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_list_yt_links(data):
    with yt_db() as conn: rows = conn.execute("SELECT token, used FROM links ORDER BY rowid DESC LIMIT 50").fetchall()
    base_url = request.host_url.rstrip('/')
    return jsonify({"status": "success", "links": [{"token": r[0], "used": bool(r[1]), "url": f"{base_url}/yt/{r[0]}"} for r in rows]})

@app.route("/admin/youtube/generate_bulk", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_generate_bulk_links(data):
    count = min(int(data.get("count", 1)), 50)
    generated, base_url = [], request.host_url.rstrip('/')
    with yt_db() as conn:
        for _ in range(count):
            new_token = str(uuid.uuid4().hex)[:8]
            conn.execute("INSERT INTO links (token, used) VALUES (?, 0)", (new_token,))
            generated.append(f"{base_url}/yt/{new_token}")
    return jsonify({"status": "success", "links": generated})

@app.route("/admin/revoke-and-issue", methods=["POST"])
@admin_required("غير مصرح")
def admin_revoke_and_issue(data):
    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub: return error("الرابط مطلوب", 400)
    with mappings_store.transaction() as mappings, blocked_store.transaction() as blocked:
        original_target = mappings.get(current_sub, current_sub)
        blocked[:] = [b for b in blocked if blocked_id(b) != current_sub]
        blocked.insert(0, {"id": current_sub, "date": datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")})
        new_sub_id = str(uuid.uuid4())
        mappings[new_sub_id] = original_target
    return jsonify({"status": "success", "new_sub_id": new_sub_id, "new_link": f"{settings.otp_link_base}{new_sub_id}", "blocked": blocked})

@app.route("/admin/unblock", methods=["POST"])
@admin_required("غير مصرح")
def admin_unblock(data):
    target_id = clean_sub_id(data.get("sub_id", ""))
    with blocked_store.transaction() as blocked: blocked[:] = [b for b in blocked if blocked_id(b) != target_id]
    return jsonify({"status": "success", "message": "تمت استعادة الصلاحية", "blocked": blocked})

@app.route("/admin/list-blocked", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_list_blocked(data): return jsonify({"status": "success", "blocked": load_blocked()})

@app.errorhandler(404)
def _not_found(_): return error("Not found", 404)
@app.errorhandler(405)
def _method_not_allowed(_): return error("Method not allowed", 405)
@app.errorhandler(Exception)
def _unhandled(exc: Exception): return error("Internal server error", 500)

@app.route("/", methods=["GET"])
def health_check(): return jsonify({"status": "active", "service": "Elevaraa Core Unified Server"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
