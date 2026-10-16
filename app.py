"""Elevaraa Core Unified Server (API Mode for Custom Domain) - hardened build.

Run in production (single process, many threads, keeps in-memory limiters accurate):
    gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT server:app
"""
from __future__ import annotations

import atexit
import copy
import datetime
import hashlib
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
from email.utils import parsedate_to_datetime
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

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("elevaraa")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
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
    yt_link_base: str = "https://elevara1.shop/yt/?token="

    allowed_origins: tuple[str, ...] = ("*",)
    trust_proxy: bool = True

    # New (all optional, defaults keep the old behaviour)
    rate_limit_per_min: int = 120      # public endpoints, per IP. 0 = disabled
    otp_max_age: int = 0               # seconds; 0 = don't check email age
    otp_cache_ttl: int = 2             # seconds; coalesces polling into one IMAP hit
    imap_max_connections: int = 4      # concurrent IMAP sessions per process

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
            rate_limit_per_min=_env_int("RATE_LIMIT_PER_MIN", 120),
            otp_max_age=_env_int("OTP_MAX_AGE", 0),
            otp_cache_ttl=_env_int("OTP_CACHE_TTL", 2),
            imap_max_connections=max(1, _env_int("IMAP_MAX_CONNECTIONS", 4)),
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
Config = settings

SUB_ID_PATTERN = re.compile(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", re.I)
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
MAX_BODY_CHARS = 500_000  # cap on text scanned per email

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.json.ensure_ascii = False
if settings.trust_proxy:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
CORS(app, resources={r"/*": {"origins": "*" if "*" in settings.allowed_origins else list(settings.allowed_origins)}})

try:
    from canva import canva_bp

    app.register_blueprint(canva_bp)
except ImportError:
    pass  # optional module
except Exception:
    log.exception("Failed to register canva blueprint")


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
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith(("/get-", "/api/", "/admin/")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# Rate limiting (in-memory, per process)
# --------------------------------------------------------------------------- #
class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: int):
        self.max_events, self.window = max_events, window_seconds
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._ops = 0

    def _prune(self, key: str, now: float) -> deque[float] | None:
        events = self._events.get(key)
        if not events:
            return None
        cutoff = now - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            del self._events[key]
            return None
        return events

    def _maybe_sweep(self, now: float) -> None:
        # Keeps memory bounded when many one-off IPs hit the service.
        self._ops += 1
        if self._ops % 512:
            return
        for key in list(self._events):
            self._prune(key, now)

    def is_limited(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic()) or ()) >= self.max_events

    def record(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now)
            self._events[key].append(now)
            self._maybe_sweep(now)

    def hit(self, key: str) -> bool:
        """Atomically check and record. Returns True if the request is allowed."""
        with self._lock:
            now = time.monotonic()
            if len(self._prune(key, now) or ()) >= self.max_events:
                return False
            self._events[key].append(now)
            self._maybe_sweep(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


FailureLimiter = SlidingWindowLimiter  # backwards-compatible name
admin_limiter = SlidingWindowLimiter(max_events=10, window_seconds=300)
yt_limiter = SlidingWindowLimiter(max_events=30, window_seconds=600)


def client_ip() -> str:
    return request.remote_addr or "unknown"


def rate_limited(limiter: SlidingWindowLimiter | None) -> Callable:
    """Per-IP request throttle for public endpoints."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if limiter is not None and not limiter.hit(client_ip()):
                return error("محاولات كثيرة، حاول لاحقاً.", 429)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def _public_limiter() -> SlidingWindowLimiter | None:
    if settings.rate_limit_per_min <= 0:
        return None
    return SlidingWindowLimiter(settings.rate_limit_per_min, 60)


otp_rl, netflix_rl, disney_rl = _public_limiter(), _public_limiter(), _public_limiter()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def admin_required(unauthorized_message: str) -> Callable:
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
            supplied = str(data.get("password") or request.headers.get("X-Admin-Password", ""))
            # Compare fixed-length digests so neither content nor length leaks via timing.
            if not hmac.compare_digest(_digest(supplied), _digest(settings.admin_password)):
                admin_limiter.record(ip)
                return error(unauthorized_message, 401)
            admin_limiter.reset(ip)
            return view(data, *args, **kwargs)

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
# JSON storage (atomic, locked, never silently loses data)
# --------------------------------------------------------------------------- #
class JsonStore:
    def __init__(self, path: str, default: Any):
        self.path, self.default, self._lock = path, default, threading.Lock()

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

    def _quarantine(self) -> None:
        """Move an unreadable file aside so the next write can't destroy evidence."""
        backup = f"{self.path}.corrupt-{int(time.time())}"
        try:
            os.replace(self.path, backup)
            log.error("Corrupt store %s moved to %s", self.path, backup)
        except OSError:
            log.exception("Could not quarantine corrupt store %s", self.path)

    def _read(self) -> Any:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return copy.deepcopy(self.default)
        except (OSError, ValueError):
            log.exception("Failed reading %s", self.path)
            self._quarantine()
            return copy.deepcopy(self.default)
        if not isinstance(data, type(self.default)):
            log.error("Unexpected data type in %s", self.path)
            self._quarantine()
            return copy.deepcopy(self.default)
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
        except Exception:
            log.exception("Failed writing %s", self.path)
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise  # caller must know the write failed (no false "success")

    def load(self) -> Any:
        with self._guard():
            return self._read()

    def save(self, data: Any) -> None:
        with self._guard():
            self._write(data)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._guard():
            data = self._read()
            yield data  # an exception here skips the write
            self._write(data)


blocked_store, mappings_store = JsonStore(settings.blocklist_file, []), JsonStore(settings.mapping_file, {})


def load_blocked() -> list:
    return blocked_store.load()


def load_mappings() -> dict:
    return mappings_store.load()


def blocked_id(entry: Any) -> Any:
    return entry.get("id") if isinstance(entry, dict) else entry


def clean_sub_id(raw: Any) -> str:
    match = SUB_ID_PATTERN.search(str(raw or ""))
    return match.group(1).lower() if match else str(raw or "").strip().lower()


# --------------------------------------------------------------------------- #
# YouTube link database
# --------------------------------------------------------------------------- #
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
            conn.execute("CREATE TABLE IF NOT EXISTS links (token TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0)")
    except Exception:
        log.exception("Could not initialise YouTube DB at %s", settings.youtube_db)


init_yt_db()


def claim_yt_token(token: str) -> bool:
    # Single atomic UPDATE: two simultaneous requests can never both claim a token.
    with yt_db() as conn:
        return conn.execute("UPDATE links SET used = 1 WHERE token = ? AND used = 0", (token,)).rowcount == 1


def create_yt_tokens(count: int) -> list[str]:
    tokens: list[str] = []
    with yt_db() as conn:
        while len(tokens) < count:
            t = secrets.token_hex(8)
            if conn.execute("INSERT OR IGNORE INTO links (token, used) VALUES (?, 0)", (t,)).rowcount == 1:
                tokens.append(t)
    return tokens


# --------------------------------------------------------------------------- #
# IMAP helpers
# --------------------------------------------------------------------------- #
IMAP_ERRORS = (IMAP4.error, OSError, TimeoutError)  # OSError covers ssl/socket errors
_imap_slots = threading.BoundedSemaphore(settings.imap_max_connections)


@contextmanager
def imap_session(account: str, password: str) -> Iterator[IMAP4_SSL]:
    # Gmail caps concurrent connections; queue briefly instead of failing under load.
    if not _imap_slots.acquire(timeout=settings.imap_timeout):
        raise TimeoutError("IMAP busy")
    mail = None
    try:
        mail = IMAP4_SSL(settings.imap_server, settings.imap_port, timeout=settings.imap_timeout)
        mail.login(account, password)
        yield mail
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
        _imap_slots.release()


def decode_subject(raw_subject: Any) -> str:
    try:
        parts = decode_header(str(raw_subject or ""))
    except Exception:
        return str(raw_subject or "")
    out: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(charset or "utf-8", errors="ignore"))
            except LookupError:
                out.append(text.decode("utf-8", errors="ignore"))
        else:
            out.append(str(text))
    return "".join(out)


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
    except (LookupError, UnicodeError):
        return payload.decode("utf-8", errors="ignore")


def extract_message_texts(msg: Message) -> tuple[str, str]:
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode_part(part)[:MAX_BODY_CHARS])
        elif ctype == "text/html":
            raw = _decode_part(part)[:MAX_BODY_CHARS]
            html.append(HTML_TAG_PATTERN.sub(" ", HTML_STYLE_PATTERN.sub(" ", raw)))
    return "\n".join(plain), "\n".join(html)


def _is_too_old(msg: Message) -> bool:
    if settings.otp_max_age <= 0:
        return False
    try:
        sent = parsedate_to_datetime(msg.get("Date"))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return False  # unknown date: don't discard a possibly valid code
    age = (datetime.datetime.now(datetime.timezone.utc) - sent).total_seconds()
    return age > settings.otp_max_age


def find_osn_otp(mail: IMAP4_SSL) -> str | None:
    st, _ = mail.select("INBOX", readonly=True)
    if st != "OK":
        return None
    st, data = mail.search(None, "ALL")
    if st != "OK" or not data or not data[0]:
        return None
    for msg_id in reversed(data[0].split()[-OSN_SCAN_LIMIT:]):
        st, msg_data = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if st != "OK":
            continue
        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            msg = message_from_bytes(part[1])
            if "osn" not in str(msg.get("From", "")).lower():
                continue
            if _is_too_old(msg):
                continue
            match = OTP_SUBJECT_PATTERN.search(decode_subject(msg.get("Subject")))
            if match:
                return match.group(1)
    return None


# Short-lived cache so many clients polling /get-otp share one IMAP round trip.
_otp_cache: dict[str, Any] = {"value": None, "ts": 0.0}
_otp_cache_lock = threading.Lock()


def _cached_osn_otp() -> str | None:
    ttl = settings.otp_cache_ttl
    with _otp_cache_lock:  # also serialises concurrent misses into a single fetch
        if ttl > 0 and time.monotonic() - _otp_cache["ts"] < ttl:
            return _otp_cache["value"]
        with imap_session(settings.email_account, settings.email_password) as mail:
            value = find_osn_otp(mail)
        _otp_cache.update(value=value, ts=time.monotonic())
        return value


@app.route("/get-otp", methods=["GET"])
@rate_limited(otp_rl)
def get_otp():
    if not settings.osn_configured:
        return error("الخدمة غير مفعّلة.", 503)
    try:
        otp = _cached_osn_otp()
        if otp:
            return jsonify({"status": "success", "otp": otp, "length": len(otp)})
    except IMAP_ERRORS:
        log.warning("OSN IMAP lookup failed", exc_info=True)
    return jsonify({"status": "waiting", "otp": None, "length": 0})


# --------------------------------------------------------------------------- #
# Netflix
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetflixEndpoint:
    key: str
    path: str
    code_fields: tuple
    email_fields: tuple


NETFLIX_ENDPOINTS = (
    NetflixEndpoint("signin_code", "signin-code", ("code", "signInCode"), ("email", "accountEmail")),
    NetflixEndpoint("temp_code", "temp-code", ("code", "tempCode"), ("email",)),
    NetflixEndpoint("login_code", "login-verification-code", ("code", "loginVerificationCode", "url"), ("email",)),
)
_netflix_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="netflix")
atexit.register(_netflix_pool.shutdown, wait=False)


def _fetch_netflix(ep: NetflixEndpoint, headers: dict) -> dict | None:
    try:
        resp = http.get(f"{settings.netflix_api}/{ep.path}", headers=headers, timeout=settings.netflix_timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return {
            "code": next((data[f] for f in ep.code_fields if data.get(f)), None),
            "email": next((data[f] for f in ep.email_fields if data.get(f)), None),
        }
    except (requests.RequestException, ValueError):
        log.debug("Netflix endpoint %s failed", ep.path, exc_info=True)
        return None


@app.route("/get-netflix", methods=["GET"])
@rate_limited(netflix_rl)
def get_netflix():
    sub_id = clean_sub_id(request.args.get("sub_id"))
    if not sub_id or not SAFE_ID_PATTERN.fullmatch(sub_id):
        return error("Invalid ID", 400)
    if sub_id in {blocked_id(b) for b in load_blocked()}:
        return jsonify({"status": "blocked", "message": "انتهت صلاحية هذا الرابط."}), 403

    bk = load_mappings().get(sub_id, sub_id)
    result: dict[str, Any] = {
        "status": "success", "email": None, "signin_code": None,
        "temp_code": None, "login_code": None, "approval_url": None,
    }
    headers = {
        "accept": "application/json",
        "referer": f"https://tv.ostories.me/?subscriptionId={bk}",
        "user-agent": "Mozilla/5.0",
        "x-api-key": bk,
    }
    wait = settings.netflix_timeout * 3 + 2  # covers the 2 retries configured on the session
    futures = [(ep, _netflix_pool.submit(_fetch_netflix, ep, headers)) for ep in NETFLIX_ENDPOINTS]
    for ep, future in futures:
        try:
            out = future.result(timeout=wait)
        except Exception:
            log.warning("Netflix endpoint %s timed out or crashed", ep.path, exc_info=True)
            continue
        if out:
            result[ep.key] = out["code"]
            if not result["email"]:
                result["email"] = out["email"]
    for key in ("signin_code", "temp_code", "login_code"):
        match = URL_PATTERN.search(str(result.get(key) or ""))
        if match:
            result["approval_url"] = match.group(1)
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Disney
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4)
def _disney_alias_pattern(account: str) -> re.Pattern[str]:
    local, _, domain = account.partition("@")
    return re.compile(rf"{re.escape(local)}\+[a-z0-9._-]{{1,64}}@{re.escape(domain)}", re.I)


@app.route("/get-disney", methods=["POST"])
@rate_limited(disney_rl)
def fetch_disney_code():
    if not settings.disney_configured:
        return error("الخدمة غير مفعّلة.", 503)
    body = request.get_json(silent=True)
    target_email = str((body if isinstance(body, dict) else {}).get("email", "")).strip().lower()
    if not _disney_alias_pattern(settings.disney_email_account.lower()).fullmatch(target_email):
        return error("إيميل غير صالح.", 400)
    try:
        with imap_session(settings.disney_email_account, settings.disney_email_password) as mail:
            st, _ = mail.select("INBOX")
            if st == "OK":
                st, data = mail.search(None, f'(TO "{target_email}" FROM "MyDisney")')
                if st == "OK" and data and data[0]:
                    latest_id = data[0].split()[-1]
                    st, msg_data = mail.fetch(latest_id, "(RFC822)")
                    if st == "OK":
                        for part in msg_data:
                            if not isinstance(part, tuple):
                                continue
                            plain, html = extract_message_texts(message_from_bytes(part[1]))
                            for text in (plain, html):
                                match = DISNEY_CODE_PATTERN.search(text)
                                if match:
                                    mail.store(latest_id, "+FLAGS", "\\Deleted")
                                    mail.expunge()
                                    return jsonify({"status": "success", "code": match.group(0)})
                    return jsonify({"status": "error", "message": "لم يتم التعرف على الكود."})
    except IMAP_ERRORS:
        log.warning("Disney IMAP lookup failed", exc_info=True)
        return error("تعذر الاتصال بالبريد.", 500)
    return jsonify({"status": "error", "message": "لم يتم العثور على رسالة."})


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #
@app.route("/api/yt/<token>", methods=["GET"])
def youtube_magic_link_api(token: str):
    if not settings.youtube_configured:
        return error("الخدمة غير مفعّلة", 503)
    ip = client_ip()
    if yt_limiter.is_limited(ip):
        return error("محاولات كثيرة، حاول لاحقاً.", 429)
    try:
        valid = bool(TOKEN_PATTERN.fullmatch(token)) and claim_yt_token(token)
    except sqlite3.Error:
        log.exception("YouTube DB error while claiming token")
        return error("Internal server error", 500)
    if not valid:
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
        rows = conn.execute("SELECT token, used FROM links ORDER BY rowid DESC LIMIT ?", (YT_LIST_LIMIT,)).fetchall()
    return jsonify({
        "status": "success",
        "links": [{"token": r["token"], "used": bool(r["used"]), "url": f"{settings.yt_link_base}{r['token']}"} for r in rows],
    })


@app.route("/admin/youtube/generate_bulk", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_generate_bulk_links(data: dict):
    try:
        count = max(1, min(int(data.get("count", 1)), MAX_BULK_LINKS))
    except (TypeError, ValueError):
        return error("عدد غير صالح", 400)
    return jsonify({"status": "success", "links": [f"{settings.yt_link_base}{t}" for t in create_yt_tokens(count)]})


# --------------------------------------------------------------------------- #
# Admin: Netflix link management
# --------------------------------------------------------------------------- #
@app.route("/admin/revoke-and-issue", methods=["POST"])
@admin_required("غير مصرح")
def admin_revoke_and_issue(data: dict):
    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub or not SAFE_ID_PATTERN.fullmatch(current_sub):
        return error("الرابط مطلوب", 400)
    new_sub_id = str(uuid.uuid4())
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %I:%M %p")
    # The inner store (mappings) is written first: if the second write fails we are left
    # with a harmless unused mapping, never a revoked link without a replacement.
    with blocked_store.transaction() as blocked, mappings_store.transaction() as mappings:
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


# --------------------------------------------------------------------------- #
# Startup diagnostics
# --------------------------------------------------------------------------- #
def _startup_checks() -> None:
    for name, ok in (
        ("admin", settings.admin_configured), ("osn", settings.osn_configured),
        ("disney", settings.disney_configured), ("youtube", settings.youtube_configured),
    ):
        if not ok:
            log.warning("Service '%s' is not configured and will return 503", name)
    if "*" in settings.allowed_origins:
        log.warning("ALLOWED_ORIGINS is '*'. Restrict it to your frontend domain in production.")
    if settings.admin_configured and len(settings.admin_password) < 12:
        log.warning("ADMIN_PASSWORD is short; use at least 12 characters.")


_startup_checks()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=_env_int("PORT", 5000))
