from flask import Flask, jsonify, request
from flask_cors import CORS
import imaplib
import email
from email.header import decode_header
import re
import requests
import json
import os
import uuid
import datetime

# استدعاء ملف كانفا المستقل
try:
    from canva import canva_bp
except Exception as e:
    canva_bp = None
    print("Canva module import error:", e)

app = Flask(__name__)
CORS(app)

# تشغيل مسار كانفا إن وُجد
if canva_bp:
    app.register_blueprint(canva_bp)

ADMIN_PASSWORD = "admin@elevaraa1451"
BLOCKLIST_FILE = "blocked_subs.json"
MAPPING_FILE = "sub_mappings.json"

def clean_sub_id(raw_str):
    if not raw_str:
        return ""
    match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', raw_str, re.I)
    if match:
        return match.group(1).lower()
    return raw_str.strip().lower()

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                return json.load(f)
        except Exception:
            return {} if "mappings" in filepath else []
    return {} if "mappings" in filepath else []

def save_json(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Error saving {filepath}:", e)

# ==========================================
# مسار فحص النشاط والحفاظ على السيرفر 24/7
# ==========================================
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({
        "status": "active",
        "service": "Elevaraa Core Unified Server",
        "features": ["Netflix OTP", "OSN OTP", "Admin Dashboard", "Canva Auto-Invite"]
    }), 200

# ==========================================
# 1. نظام OSN (قراءة الأكواد من Gmail)
# ==========================================
IMAP_SERVER = "imap.gmail.com"
EMAIL_ACCOUNT = "elevaraa8@gmail.com"
EMAIL_PASSWORD = "zcfuvmpgibqatcer"

def get_genius_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("INBOX")
        status, messages = mail.search(None, "ALL")
        if status != "OK" or not messages[0]:
            return None, None

        email_ids = messages[0].split()
        for e_id in reversed(email_ids[-15:]):
            status, msg_data = mail.fetch(e_id, '(BODY.PEEK[HEADER])')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    if "osn" not in str(msg.get("From", "")).lower():
                        continue
                    subject = ""
                    raw_subj = msg.get("Subject", "")
                    if raw_subj:
                        for text, charset in decode_header(raw_subj):
                            subject += text.decode(charset or 'utf-8', errors='ignore') if isinstance(text, bytes) else str(text)
                    m = re.search(r'\b(\d{4})\b', subject)
                    if m:
                        mail.logout()
                        return m.group(1), 4
        mail.logout()
    except Exception as e:
        print("Error in OSN IMAP:", e)
    return None, None

@app.route('/get-otp', methods=['GET'])
def get_otp():
    code, l = get_genius_otp()
    if code:
        return jsonify({"status": "success", "otp": code, "length": l})
    return jsonify({"status": "waiting", "otp": None, "length": 0})

# ==========================================
# 2. نظام نتفليكس (جلب الأكواد وروابط الموافقة)
# ==========================================
@app.route('/get-netflix', methods=['GET'])
def get_netflix():
    sub_id = clean_sub_id(request.args.get('sub_id'))
    if not sub_id:
        return jsonify({"status": "error", "message": "ID required"}), 400

    blocked = load_json(BLOCKLIST_FILE)
    if sub_id in [b.get("id") if isinstance(b, dict) else b for b in blocked]:
        return jsonify({"status": "blocked", "message": "نعتذر، انتهت صلاحية هذا الرابط."}), 403

    mappings = load_json(MAPPING_FILE)
    backend_key = mappings.get(sub_id, sub_id)

    headers = {
        'accept': 'application/json',
        'referer': f'https://tv.ostories.me/?subscriptionId={backend_key}',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'x-api-key': backend_key
    }
    base_api = "https://tv.ostories.me/api/dealer-subscriptions"
    result = {"status": "success", "email": None, "signin_code": None, "temp_code": None, "login_code": None, "approval_url": None}

    with requests.Session() as s:
        s.headers.update(headers)
        try:
            r1 = s.get(f"{base_api}/signin-code", timeout=4)
            if r1.status_code == 200:
                d1 = r1.json()
                result["signin_code"] = d1.get("code") or d1.get("signInCode")
                result["email"] = d1.get("email") or d1.get("accountEmail")
        except Exception:
            pass
        try:
            r2 = s.get(f"{base_api}/temp-code", timeout=4)
            if r2.status_code == 200:
                d2 = r2.json()
                result["temp_code"] = d2.get("code") or d2.get("tempCode")
                if not result["email"]:
                    result["email"] = d2.get("email")
        except Exception:
            pass
        try:
            r3 = s.get(f"{base_api}/login-verification-code", timeout=4)
            if r3.status_code == 200:
                d3 = r3.json()
                result["login_code"] = d3.get("code") or d3.get("loginVerificationCode") or d3.get("url")
                if not result["email"]:
                    result["email"] = d3.get("email")
        except Exception:
            pass

    for k in ["signin_code", "temp_code", "login_code"]:
        m_url = re.search(r'(https?://[^\s]+)', str(result.get(k) or ""))
        if m_url:
            result["approval_url"] = m_url.group(1)

    return jsonify(result)

# ==========================================
# 3. لوحة الإدارة وتوليد وإلغاء الروابط
# ==========================================
@app.route('/admin/list-blocked', methods=['POST'])
def admin_list_blocked():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "رمز المرور غير صحيح"}), 401
    return jsonify({"status": "success", "blocked": load_json(BLOCKLIST_FILE)})

@app.route('/admin/revoke-and-issue', methods=['POST'])
def admin_revoke_and_issue():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "غير مصرح"}), 401

    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub:
        return jsonify({"status": "error", "message": "الرابط مطلوب"}), 400

    mappings = load_json(MAPPING_FILE)
    original_target = mappings.get(current_sub, current_sub)

    blocked = load_json(BLOCKLIST_FILE)
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    blocked = [b for b in blocked if (b.get("id") if isinstance(b, dict) else b) != current_sub]
    blocked.insert(0, {"id": current_sub, "date": now_time})
    save_json(BLOCKLIST_FILE, blocked)

    new_sub_id = str(uuid.uuid4())
    mappings[new_sub_id] = original_target
    save_json(MAPPING_FILE, mappings)

    return jsonify({
        "status": "success",
        "new_sub_id": new_sub_id,
        "new_link": f"https://elevara1.shop/otp.netflix/?subscriptionId={new_sub_id}",
        "blocked": blocked
    })

@app.route('/admin/unblock', methods=['POST'])
def admin_unblock():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "غير مصرح"}), 401
    target_id = clean_sub_id(data.get("sub_id", ""))
    blocked = load_json(BLOCKLIST_FILE)
    blocked = [b for b in blocked if (b.get("id") if isinstance(b, dict) else b) != target_id]
    save_json(BLOCKLIST_FILE, blocked)
    return jsonify({"status": "success", "message": "تمت استعادة الصلاحية", "blocked": blocked})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
