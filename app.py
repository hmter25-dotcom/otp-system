"""Elevaraa Core Unified Server.

يدعم النظام الآن المتغيرات البيئية المخفية للأمان، مع وجود قيم احتياطية 
لضمان عدم توقف السيرفر في حال عدم إعدادها في Render.
"""
import datetime
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from email import message_from_bytes
from email.header import decode_header
from functools import wraps
from imaplib import IMAP4_SSL

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

# ==========================================
# الإعدادات الأساسية
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("elevaraa")

class Config:
    # استخدام المتغيرات البيئية أولاً، وإن لم توجد نستخدم القيم الافتراضية
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin@elevaraa1451")
    
    # إعدادات OSN
    EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "elevaraa8@gmail.com")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "zcfuvmpgibqatcer")
    
    # إعدادات Disney (تم دمجها بأمان)
    DISNEY_EMAIL_ACCOUNT = os.environ.get("DISNEY_EMAIL_ACCOUNT", "elevaraa11@gmail.com")
    DISNEY_EMAIL_PASSWORD = os.environ.get("DISNEY_EMAIL_PASSWORD", "qkpxilcjdwjxzewx")
    
    IMAP_SERVER = "imap.gmail.com"
    IMAP_PORT = 993
    BLOCKLIST_FILE = "blocked_subs.json"
    MAPPING_FILE = "sub_mappings.json"
    NETFLIX_API = "https://tv.ostories.me/api/dealer-subscriptions"
    NETFLIX_TIMEOUT = 4
    OTP_LINK_BASE = "https://elevara1.shop/otp.netflix/?subscriptionId="

SUB_ID_PATTERN = re.compile(
    r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", re.I
)
URL_PATTERN = re.compile(r"(https?://[^\s]+)")

app = Flask(__name__)
CORS(app)

# تشغيل مسار كانفا إن وُجد
try:
    from canva import canva_bp
    app.register_blueprint(canva_bp)
except Exception:
    log.exception("Canva module import error")

# ==========================================
# أدوات مساعدة
# ==========================================
_file_lock = threading.Lock()

def clean_sub_id(raw):
    if not raw:
        return ""
    match = SUB_ID_PATTERN.search(raw)
    return match.group(1).lower() if match else raw.strip().lower()

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to read %s", path)
        return default

def save_json(path, data):
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, delete=False, encoding="utf-8", suffix=".tmp"
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    except OSError:
        log.exception("Error saving %s", path)

def load_blocked():
    return load_json(Config.BLOCKLIST_FILE, [])

def load_mappings():
    return load_json(Config.MAPPING_FILE, {})

def blocked_id(entry):
    return entry.get("id") if isinstance(entry, dict) else entry

def error(message, status):
    return jsonify({"status": "error", "message": message}), status

def admin_required(unauthorized_message):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            data = request.get_json(silent=True) or {}
            supplied = str(data.get("password", "")).encode()
            if not hmac.compare_digest(supplied, Config.ADMIN_PASSWORD.encode()):
                return error(unauthorized_message, 401)
            return view(data, *args, **kwargs)
        return wrapper
    return decorator

# ==========================================
# فحص النشاط
# ==========================================
@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "active",
        "service": "Elevaraa Core Unified Server",
        "features": ["Netflix OTP", "OSN OTP", "Disney OTP",
                     "Admin Dashboard", "Canva Auto-Invite"],
    }), 200

# ==========================================
# 1. نظام OSN
# ==========================================
def _decode_subject(raw_subject):
    parts = []
    for text, charset in decode_header(raw_subject or ""):
        if isinstance(text, bytes):
            parts.append(text.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(str(text))
    return "".join(parts)

def get_genius_otp():
    mail = None
    try:
        mail = IMAP4_SSL(Config.IMAP_SERVER, Config.IMAP_PORT)
        mail.login(Config.EMAIL_ACCOUNT, Config.EMAIL_PASSWORD)
        mail.select("INBOX")
        status, messages = mail.search(None, "ALL")
        if status != "OK" or not messages[0]:
            return None, None

        for e_id in reversed(messages[0].split()[-15:]):
            _, msg_data = mail.fetch(e_id, "(BODY.PEEK[HEADER])")
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = message_from_bytes(part[1])
                if "osn" not in str(msg.get("From", "")).lower():
                    continue
                match = re.search(r"\b(\d{4})\b", _decode_subject(msg.get("Subject")))
                if match:
                    return match.group(1), 4
    except Exception:
        log.exception("Error in OSN IMAP")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    return None, None

@app.route("/get-otp", methods=["GET"])
def get_otp():
    code, length = get_genius_otp()
    if code:
        return jsonify({"status": "success", "otp": code, "length": length})
    return jsonify({"status": "waiting", "otp": None, "length": 0})

# ==========================================
# 2. نظام نتفليكس
# ==========================================
NETFLIX_ENDPOINTS = (
    ("signin_code", "signin-code", ("code", "signInCode"), ("email", "accountEmail")),
    ("temp_code", "temp-code", ("code", "tempCode"), ("email",)),
    ("login_code", "login-verification-code",
     ("code", "loginVerificationCode", "url"), ("email",)),
)

def _first_value(data, keys):
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None

@app.route("/get-netflix", methods=["GET"])
def get_netflix():
    sub_id = clean_sub_id(request.args.get("sub_id"))
    if not sub_id:
        return error("ID required", 400)

    if sub_id in {blocked_id(b) for b in load_blocked()}:
        return jsonify({"status": "blocked",
                        "message": "نعتذر، انتهت صلاحية هذا الرابط."}), 403

    backend_key = load_mappings().get(sub_id, sub_id)
    headers = {
        "accept": "application/json",
        "referer": f"https://tv.ostories.me/?subscriptionId={backend_key}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "x-api-key": backend_key,
    }
    result = {"status": "success", "email": None, "signin_code": None,
              "temp_code": None, "login_code": None, "approval_url": None}

    with requests.Session() as session:
        session.headers.update(headers)
        for key, path, code_fields, email_fields in NETFLIX_ENDPOINTS:
            try:
                resp = session.get(f"{Config.NETFLIX_API}/{path}",
                                   timeout=Config.NETFLIX_TIMEOUT)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                result[key] = _first_value(data, code_fields)
                if not result["email"]:
                    result["email"] = _first_value(data, email_fields)
            except (requests.RequestException, ValueError):
                log.warning("Netflix endpoint failed: %s", path)

    for key in ("signin_code", "temp_code", "login_code"):
        match = URL_PATTERN.search(str(result.get(key) or ""))
        if match:
            result["approval_url"] = match.group(1)

    return jsonify(result)

# ==========================================
# 3. لوحة الإدارة
# ==========================================
@app.route("/admin/list-blocked", methods=["POST"])
@admin_required("رمز المرور غير صحيح")
def admin_list_blocked(data):
    return jsonify({"status": "success", "blocked": load_blocked()})

@app.route("/admin/revoke-and-issue", methods=["POST"])
@admin_required("غير مصرح")
def admin_revoke_and_issue(data):
    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub:
        return error("الرابط مطلوب", 400)

    with _file_lock:
        mappings = load_mappings()
        original_target = mappings.get(current_sub, current_sub)

        now = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
        blocked = [b for b in load_blocked() if blocked_id(b) != current_sub]
        blocked.insert(0, {"id": current_sub, "date": now})
        save_json(Config.BLOCKLIST_FILE, blocked)

        new_sub_id = str(uuid.uuid4())
        mappings[new_sub_id] = original_target
        save_json(Config.MAPPING_FILE, mappings)

    return jsonify({
        "status": "success",
        "new_sub_id": new_sub_id,
        "new_link": f"{Config.OTP_LINK_BASE}{new_sub_id}",
        "blocked": blocked,
    })

@app.route("/admin/unblock", methods=["POST"])
@admin_required("غير مصرح")
def admin_unblock(data):
    target_id = clean_sub_id(data.get("sub_id", ""))
    with _file_lock:
        blocked = [b for b in load_blocked() if blocked_id(b) != target_id]
        save_json(Config.BLOCKLIST_FILE, blocked)
    return jsonify({"status": "success", "message": "تمت استعادة الصلاحية",
                    "blocked": blocked})

# ==========================================
# 4. نظام Disney+
# ==========================================
def get_disney_code(target_email):
    mail = None
    try:
        mail = IMAP4_SSL(Config.IMAP_SERVER, Config.IMAP_PORT)
        mail.login(Config.DISNEY_EMAIL_ACCOUNT, Config.DISNEY_EMAIL_PASSWORD)
        mail.select("INBOX")

        status, messages = mail.search(None, f'(TO "{target_email}" FROM "MyDisney")')
        if status != "OK" or not messages[0]:
            return {"status": "error", "message": "لم يتم العثور على رسالة. تأكد أنك طلبت الكود من تطبيق ديزني."}

        latest_email_id = messages[0].split()[-1]
        _, msg_data = mail.fetch(latest_email_id, "(RFC822)")

        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            
            msg = message_from_bytes(part[1])
            body = ""
            
            if msg.is_multipart():
                for subpart in msg.walk():
                    if subpart.get_content_type() in ["text/plain", "text/html"]:
                        try:
                            body = subpart.get_payload(decode=True).decode('utf-8', errors='ignore')
                            break
                        except Exception:
                            pass
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

            match = re.search(r'\b\d{6}\b', body)
            if match:
                code = match.group(0)
                mail.store(latest_email_id, '+FLAGS', '\\Deleted')
                mail.expunge()
                return {"status": "success", "code": code}
            else:
                return {"status": "error", "message": "تم العثور على رسالة ولكن لم يتم التعرف على الكود."}

        return {"status": "error", "message": "خطأ غير متوقع أثناء قراءة الرسالة."}
        
    except Exception:
        log.exception("Error in Disney IMAP")
        return {"status": "error", "message": "تعذر الاتصال بالبريد الإلكتروني."}
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

@app.route("/get-disney", methods=["POST"])
def fetch_disney_code():
    data = request.get_json(silent=True) or {}
    target_email = data.get("email", "").strip().lower()

    if not target_email:
        return error("يرجى إدخال الإيميل.", 400)

    if "elevaraa11+" not in target_email:
        return error("إيميل غير صالح. يجب أن تستخدم الإيميل الخاص بمتجرنا.", 400)

    result = get_disney_code(target_email)
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
