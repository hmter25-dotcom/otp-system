"""Elevaraa Core Unified Server (API Mode for Custom Domain).

نفس المسارات ونفس شكل الردود (JSON) بدون أي تغيير على الواجهة الأمامية.
"""
from __future__ import annotations

import atexit
import copy
import datetime
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from functools import lru_cache, wraps
from imaplib import IMAP4, IMAP4_SSL
from typing import Any, Callable, Iterator

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

try:  # قفل بين العمليات (Linux/Render). غير متاح على Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

# ==========================================
# Logging
# ==========================================
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("elevaraa")


# ==========================================
# Settings
# ==========================================
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        log.warning("Invalid integer in %s; using %s", name, default)
        return default


@dataclass(frozen=True)
class Settings:
    admin_password: str = ""
    email_account: str = ""
    email_password: str = ""
    disney_email_account: str = ""
    disney_email_password: str = ""
    youtube_secret: str = ""
    youtube_email: str = ""
    youtube_pass: str = ""

    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    imap_timeout: int = 15
    blocklist_file: str = "blocked_subs.json"
    mapping_file: str = "sub_mappings.json"
    youtube_db: str = "youtube_links.db"
    netflix_api: str = "https://tv.ostories.me/api/dealer-subscriptions"
    netflix_timeout: int = 4
    otp_link_base: str = "https://elevara1.shop/otp.netflix/?subscriptionId="
    yt_link_base: str = "https://elevaraa1.shop/yt/?token="
    allowed_origins: tuple[str, ...] = ("*",)
    trust_proxy: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _env("DATA_DIR", ".")
        os.makedirs(data_dir, exist_ok=True)
        origins = tuple(o.strip() for o in _env("ALLOWED_ORIGINS", "*").split(",") if o.strip())
        return cls(
            admin_password=_env("ADMIN_PASSWORD"),
            email_account=_env("EMAIL_ACCOUNT"),
            email_password=_env("EMAIL_PASSWORD"),
            disney_email_account=_env("DISNEY_EMAIL_ACCOUNT"),
            disney_email_password=_env("DISNEY_EMAIL_PASSWORD"),
            youtube_secret=_env("YOUTUBE_SECRET"),
            youtube_email=_env("YOUTUBE_EMAIL"),
            youtube_pass=_env("YOUTUBE_PASS"),
            imap_timeout=_env_int("IMAP_TIMEOUT", 15),
            blocklist_file=os.path.join(data_dir, "blocked_subs.json"),
            mapping_file=os.path.join(data_dir, "sub_mappings.json"),
            youtube_db=os.path.join(data_dir, "youtube_links.db"),
            netflix_timeout=_env_int("NETFLIX_TIMEOUT", 4),
            otp_link_base=_env("OTP_LINK_BASE", cls.otp_link_base),
            yt_link_base=_env("YT_LINK_BASE", cls.yt_link_base),
            allowed_origins=origins or ("*",),
            trust_proxy=_env("TRUST_PROXY", "1") != "0",
        )

    @property
    def admin_configured(self) -> bool:
        return bool(self.admin_password)

    @property
    def osn_configured(self) -> bool:
        return bool(self.email_account and self.email_password)

    @property
    def disney_configured(self) -> bool:
        return bool(self.disney_email_account and self.disney_email_password)

    @property
    def youtube_configured(self) -> bool:
        return bool(self.youtube_secret and self.youtube_email and self.youtube_pass)


settings = Settings.from_env()
Config = settings  # توافق مع الملفات الأخرى (مثل canva)

SUB_ID_PATTERN = re.compile(
    r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", re.I
)
SAFE_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
URL_PATTERN = re.compile(r"(https?://[^\s]+)")
OTP_SUBJECT_PATTERN = re.compile(r"\b(\d{4})\b")
DISNEY_CODE_PATTERN = re.compile(r"\b\d{6}\b")
HTML_STYLE_PATTERN = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.I | re.S)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

OSN_SCAN_LIMIT = 15
MAX_BULK_LINKS = 50
YT_LIST_LIMIT = 50


def _warn_config() -> None:
    checks = {
        "ADMIN_PASSWORD (admin routes)": settings.admin_configured,
        "EMAIL_ACCOUNT/EMAIL_PASSWORD (OSN)": settings.osn_configured,
        "DISNEY_EMAIL_ACCOUNT/PASSWORD (Disney)": settings.disney_configured,
        "YOUTUBE_SECRET/EMAIL/PASS (YouTube)": settings.youtube_configured,
    }
    for name, ok in checks.items():
        if not ok:
            log.warning("Not configured: %s", name)
    if "*" in settings.allowed_origins:
        log.warning("CORS is open to all origins; set ALLOWED_ORIGINS to restrict it")


_warn_config()

# ==========================================
# App
# ==========================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.json.ensure_ascii = False  # عرض العربية كما هي في الردود
if settings.trust_proxy:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]
CORS(
    app,
    resources={r"/*": {"origins": "*" if "*" in settings.allowed_origins else list(settings.allowed_origins)}},
)

try:
    from canva import canva_bp

    app.register_blueprint(canva_bp)
except ModuleNotFoundError as exc:
    if exc.name == "canva":
        log.info("Canva module not found; skipping")
    else:
        log.exception("Canva module has a missing dependency")
except Exception:
    log.exception("Canva module failed to load")


# ==========================================
# Responses, errors, security headers
# ==========================================
def error(message: str, status: int):
    return jsonify({"status": "error", "message": message}), status


@app.errorhandler(Exception)
def _handle_exception(exc: Exception):
    if isinstance(exc, HTTPException):
        return error(exc.name, exc.code or 500)
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return error("Internal server error", 500)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.path.startswith(("/get-", "/api/", "/admin/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ==========================================
# Rate limiting (failed attempts only)
# ==========================================
class FailureLimiter:
    """يحدّ المحاولات الفاشلة لكل عميل داخل نافذة زمنية."""

    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        events = self._events.get(key)
        if not events:
            self._events.pop(key, None)
            return None
        cutoff = now - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            del self._events[key]
            return None
        return events

    def is_limited(self, key: str) -> bool:
        with self._lock:
            events = self._prune(key, time.monotonic())
            return events is not None and len(events) >= self.max_failures

    def record(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now)
            self._events[key].append(now)
            if len(self._events) > 10_000:  # حماية الذاكرة
                for stale in list(self._events):
                    self._prune(stale, now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


admin_limiter = FailureLimiter(max_failures=10, window_seconds=300)
yt_limiter = FailureLimiter(max_failures=30, window_seconds=600)


def client_ip() -> str:
    return request.remote_addr or "unknown"


def admin_required(unauthorized_message: str) -> Callable:
    """يمرّر بيانات الطلب (dict) كأول وسيط للدالة المزخرفة."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if not settings.admin_configured:
                return error("Admin access is not configured", 503)
            ip = client_ip()
            if admin_limiter.is_limited(ip):
                return error("محاولات كثيرة، حاول لاحقاً.", 429)
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                data = {}
            supplied = str(data.get("password", "")).encode()
            if not hmac.compare_digest(supplied, settings.admin_password.encode()):
                admin_limiter.record(ip)
                return error(unauthorized_message, 401)
            admin_limiter.reset(ip)
            return view(data, *args, **kwargs)

        return wrapper

    return decorator


# ==========================================
# JSON store (thread-safe, process-safe, atomic)
# ==========================================
class JsonStore:
    def __init__(self, path: str, default: Any):
        self.path = path
        self.default = default
        self._lock = threading.Lock()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        with self._lock:
            if fcntl is None:
                yield
                return
            with open(f"{self.path}.lock", "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _fresh_default(self) -> Any:
        return copy.deepcopy(self.default)

    def _quarantine(self) -> None:
        backup = f"{self.path}.corrupt-{int(time.time())}"
        try:
            os.replace(self.path, backup)
            log.error("Corrupt data file moved to %s", backup)
        except OSError:
            log.exception("Could not quarantine corrupt file %s", self.path)

    def _read(self) -> Any:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return self._fresh_default()
        except json.JSONDecodeError:
            log.exception("Invalid JSON in %s", self.path)
            self._quarantine()
            return self._fresh_default()
        if not isinstance(data, type(self.default)):
            log.error("Unexpected data type in %s", self.path)
            self._quarantine()
            return self._fresh_default()
        return data

    def _write(self, data: Any) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=directory, delete=False, encoding="utf-8", suffix=".tmp"
            ) as tmp:
                tmp_name = tmp.name
                json.dump(data, tmp, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
        except OSError:
            log.exception("Error saving %s", self.path)
            if tmp_name and os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise

    def load(self) -> Any:
        return self._read()  # القراءة آمنة لأن الكتابة تتم بـ os.replace الذري

    def save(self, data: Any) -> None:
        with self._guard():
            self._write(data)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """قراءة-تعديل-كتابة تحت قفل واحد. عند حدوث استثناء لا يُحفظ شيء."""
        with self._guard():
            data = self._read()
            yield data
            self._write(data)


blocked_store = JsonStore(settings.blocklist_file, [])
mappings_store = JsonStore(settings.mapping_file, {})


def load_blocked() -> list:
    return blocked_store.load()


def load_mappings() -> dict:
    return mappings_store.load()


def blocked_id(entry: Any) -> Any:
    return entry.get("id") if isinstance(entry, dict) else entry


def clean_sub_id(raw: Any) -> str:
    text = "" if raw is None else str(raw)
    match = SUB_ID_PATTERN.search(text)
    return match.group(1).lower() if match else text.strip().lower()


# ==========================================
# YouTube links DB (burned links)
# ==========================================
@contextmanager
def yt_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.youtube_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
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
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS links "
                "(token TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0)"
            )
    except sqlite3.Error:
        log.exception("Error initializing YouTube DB")


def claim_yt_token(token: str) -> bool:
    """يحرق الرابط بعملية ذرية: ينجح مرة واحدة فقط حتى مع طلبات متزامنة."""
    with yt_db() as conn:
        cur = conn.execute("UPDATE links SET used = 1 WHERE token = ? AND used = 0", (token,))
        return cur.rowcount == 1


def create_yt_tokens(count: int) -> list[str]:
    tokens: list[str] = []
    with yt_db() as conn:
        while len(tokens) < count:
            token = secrets.token_hex(8)
            cur = conn.execute("INSERT OR IGNORE INTO links (token, used) VALUES (?, 0)", (token,))
            if cur.rowcount == 1:
                tokens.append(token)
    return tokens


init_yt_db()


# ==========================================
# IMAP / HTTP helpers
# ==========================================
@contextmanager
def imap_session(account: str, password: str) -> Iterator[IMAP4_SSL]:
    mail = IMAP4_SSL(settings.imap_server, settings.imap_port, timeout=settings.imap_timeout)
    try:
        mail.login(account, password)
        yield mail
    finally:
        try:
            mail.logout()
        except (IMAP4.error, OSError):
            pass


def decode_subject(raw_subject: str | None) -> str:
    parts = []
    for text, charset in decode_header(raw_subject or ""):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(charset or "utf-8", errors="ignore"))
            except LookupError:
                parts.append(text.decode("utf-8", errors="ignore"))
        else:
            parts.append(str(text))
    return "".join(parts)


def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3, status_forcelist=(502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
    return session


http = build_http_session()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    except LookupError:
        return payload.decode("utf-8", errors="ignore")


def extract_message_texts(msg: Message) -> tuple[str, str]:
    """يرجع (النص العادي، نص HTML بعد إزالة الوسوم)."""
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode_part(part))
        elif ctype == "text/html":
            raw = HTML_STYLE_PATTERN.sub(" ", _decode_part(part))
            html.append(HTML_TAG_PATTERN.sub(" ", raw))
    return "\n".join(plain), "\n".join(html)


# ==========================================
# OSN
# ==========================================
def find_osn_otp(mail: IMAP4_SSL) -> str | None:
    status, _ = mail.select("INBOX", readonly=True)
    if status != "OK":
        raise IMAP4.error("Cannot select INBOX")
    status, data = mail.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return None
    for msg_id in reversed(data[0].split()[-OSN_SCAN_LIMIT:]):
        _, msg_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER])")
        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            msg = message_from_bytes(part[1])
            if "osn" not in str(msg.get("From", "")).lower():
                continue
            match = OTP_SUBJECT_PATTERN.search(decode_subject(msg.get("Subject")))
            if match:
                return match.group(1)
    return None


@app.route("/get-otp", methods=["GET"])
def get_otp():
    if not settings.osn_configured:
        return error("الخدمة غير مفعّلة.", 503)
    otp = None
    try:
        with imap_session(settings.email_account, settings.email_password) as mail:
            otp = find_osn_otp(mail)
    except (IMAP4.error, OSError):
        log.exception("OSN IMAP failure")
    if otp:
        return jsonify({"status": "success", "otp": otp, "length": len(otp)})
    return jsonify({"status": "waiting", "otp": None, "length": 0})


# ==========================================
# Netflix
# ==========================================
@dataclass(frozen=True)
class NetflixEndpoint:
    key: str
    path: str
    code_fields: tuple[str, ...]
    email_fields: tuple[str, ...]


NETFLIX_ENDPOINTS = (
    NetflixEndpoint("signin_code", "signin-code", ("code", "signInCode"), ("email", "accountEmail")),
    NetflixEndpoint("temp_code", "temp-code", ("code", "tempCode"), ("email",)),
    NetflixEndpoint(
        "login_code", "login-verification-code", ("code", "loginVerificationCode", "url"), ("email",)
    ),
)
_netflix_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="netflix")
atexit.register(_netflix_pool.shutdown, wait=False)


def _first_value(data: dict, fields: tuple[str, ...]) -> Any:
    return next((data[f] for f in fields if data.get(f)), None)


def _fetch_netflix(endpoint: NetflixEndpoint, headers: dict[str, str]) -> dict[str, Any] | None:
    url = f"{settings.netflix_api}/{endpoint.path}"
    try:
        resp = http.get(url, headers=headers, timeout=settings.netflix_timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Netflix upstream '%s' failed: %s", endpoint.path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return {
        "code": _first_value(data, endpoint.code_fields),
        "email": _first_value(data, endpoint.email_fields),
    }


@app.route("/get-netflix", methods=["GET"])
def get_netflix():
    sub_id = clean_sub_id(request.args.get("sub_id"))
    if not sub_id:
        return error("ID required", 400)
    if not SAFE_ID_PATTERN.fullmatch(sub_id):
        return error("Invalid ID", 400)
    if sub_id in {blocked_id(b) for b in load_blocked()}:
        return jsonify({"status": "blocked", "message": "انتهت صلاحية هذا الرابط."}), 403

    backend_key = load_mappings().get(sub_id, sub_id)
    headers = {
        "accept": "application/json",
        "referer": f"https://tv.ostories.me/?subscriptionId={backend_key}",
        "user-agent": "Mozilla/5.0",
        "x-api-key": backend_key,
    }
    result: dict[str, Any] = {
        "status": "success", "email": None, "signin_code": None,
        "temp_code": None, "login_code": None, "approval_url": None,
    }

    # الطلبات الثلاثة تتم بالتوازي بدل التتابع (يقلل زمن الاستجابة للثلث تقريباً)
    futures = [(ep, _netflix_pool.submit(_fetch_netflix, ep, headers)) for ep in NETFLIX_ENDPOINTS]
    for endpoint, future in futures:
        outcome = future.result()
        if not outcome:
            continue
        result[endpoint.key] = outcome["code"]
        if not result["email"]:
            result["email"] = outcome["email"]

    for key in ("signin_code", "temp_code", "login_code"):
        match = URL_PATTERN.search(str(result.get(key) or ""))
        if match:
            result["approval_url"] = match.group(1)
    return jsonify(result)


# ==========================================
# Disney
# ==========================================
@lru_cache(maxsize=4)
def _disney_alias_pattern(account: str) -> re.Pattern[str]:
    local, _, domain = account.partition("@")
    return re.compile(rf"{re.escape(local)}\+[a-z0-9._-]{{1,64}}@{re.escape(domain)}", re.I)


def lookup_disney_code(target_email: str) -> tuple[str | None, str | None]:
    """يرجع (الكود، None) أو (None، سبب: not_found | unrecognized)."""
    with imap_session(settings.disney_email_account, settings.disney_email_password) as mail:
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise IMAP4.error("Cannot select INBOX")
        status, data = mail.search(None, f'(TO "{target_email}" FROM "MyDisney")')
        if status != "OK" or not data or not data[0]:
            return None, "not_found"
        latest_id = data[0].split()[-1]
        _, msg_data = mail.fetch(latest_id, "(RFC822)")
        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            plain, html = extract_message_texts(message_from_bytes(part[1]))
            # نفضّل النص العادي لتفادي التقاط أرقام من تنسيق HTML
            for text in (plain, html):
                match = DISNEY_CODE_PATTERN.search(text)
                if match:
                    mail.store(latest_id, "+FLAGS", "\\Deleted")
                    mail.expunge()
                    return match.group(0), None
        return None, "unrecognized"


@app.route("/get-disney", methods=["POST"])
def fetch_disney_code():
    if not settings.disney_configured:
        return error("الخدمة غير مفعّلة.", 503)
    payload = request.get_json(silent=True)
    raw_email = payload.get("email") if isinstance(payload, dict) else None
    target_email = str(raw_email or "").strip().lower()
    if not _disney_alias_pattern(settings.disney_email_account.lower()).fullmatch(target_email):
        return error("إيميل غير صالح.", 400)
    try:
        code, reason = lookup_disney_code(target_email)
    except (IMAP4.error, OSError):
        log.exception("Disney IMAP failure")
        return error("تعذر الاتصال بالبريد.", 500)
    if code:
        return jsonify({"status": "success", "code": code})
    messages = {
        "unrecognized": "لم يتم التعرف على الكود.",
        "not_found": "لم يتم العثور على رسالة.",
    }
    return jsonify({"status": "error", "message": messages.get(reason or "", messages["not_found"])})


# ==========================================
# YouTube API (Custom Domain)
# ==========================================
@app.route("/api/yt/<token>", methods=["GET"])
def youtube_magic_link_api(token: str):
    if not settings.youtube_configured:
        return error("الخدمة غير مفعّلة", 503)  # قبل حرق الرابط
    ip = client_ip()
    if yt_limiter.is_limited(ip):
        return error("محاولات كثيرة، حاول لاحقاً.", 429)
    if not TOKEN_PATTERN.fullmatch(token) or not claim_yt_token(token):
        yt_limiter.record(ip)
        return error("الرابط محروق أو غير صحيح", 403)
    return jsonify({
        "status": "success",
        "email": settings.youtube_email,
        "password": settings.youtube_pass,
        "secret": settings.youtube_secret,
    })


@app.route("/admin/youtube/list", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_list_yt_links(data: dict):
    with yt_db() as conn:
        rows = conn.execute(
            "SELECT token, used FROM links ORDER BY rowid DESC LIMIT ?", (YT_LIST_LIMIT,)
        ).fetchall()
    return jsonify({
        "status": "success",
        "links": [
            {"token": r["token"], "used": bool(r["used"]), "url": f"{settings.yt_link_base}{r['token']}"}
            for r in rows
        ],
    })


@app.route("/admin/youtube/generate_bulk", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_generate_bulk_links(data: dict):
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return error("عدد غير صالح", 400)
    count = max(1, min(count, MAX_BULK_LINKS))
    links = [f"{settings.yt_link_base}{t}" for t in create_yt_tokens(count)]
    return jsonify({"status": "success", "links": links})


# ==========================================
# General admin
# ==========================================
@app.route("/admin/revoke-and-issue", methods=["POST"])
@admin_required("غير مصرح")
def admin_revoke_and_issue(data: dict):
    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub or not SAFE_ID_PATTERN.fullmatch(current_sub):
        return error("الرابط مطلوب", 400)
    new_sub_id = str(uuid.uuid4())
    # التوقيت بصيغة UTC صراحةً (نفس سلوك سيرفرات Render السابق)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %I:%M %p")
    with mappings_store.transaction() as mappings, blocked_store.transaction() as blocked:
        original_target = mappings.get(current_sub, current_sub)
        blocked[:] = [b for b in blocked if blocked_id(b) != current_sub]
        blocked.insert(0, {"id": current_sub, "date": stamp})
        mappings[new_sub_id] = original_target
    return jsonify({
        "status": "success",
        "new_sub_id": new_sub_id,
        "new_link": f"{settings.otp_link_base}{new_sub_id}",
        "blocked": blocked,
    })


@app.route("/admin/unblock", methods=["POST"])
@admin_required("غير مصرح")
def admin_unblock(data: dict):
    target_id = clean_sub_id(data.get("sub_id", ""))
    if not target_id:
        return error("الرابط مطلوب", 400)
    with blocked_store.transaction() as blocked:
        blocked[:] = [b for b in blocked if blocked_id(b) != target_id]
    return jsonify({"status": "success", "message": "تمت استعادة الصلاحية", "blocked": blocked})


@app.route("/admin/list-blocked", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_list_blocked(data: dict):
    return jsonify({"status": "success", "blocked": load_blocked()})


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "active", "service": "Elevaraa Core Unified API"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_env_int("PORT", 5000))
