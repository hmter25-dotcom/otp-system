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

app = Flask(__name__)
CORS(app)

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
# 1. نظام OSN
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
        if status != "OK":
            return None, None

        email_ids = messages[0].split()
        if not email_ids:
            return None, None

        for e_id in reversed(email_ids[-15:]):
            status, msg_data = mail.fetch(e_id, '(BODY.PEEK[HEADER])')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    sender = str(msg.get("From", "")).lower()
                    if "osn" not in sender:
                        continue 
                        
                    subject = ""
                    raw_subject = msg.get("Subject", "")
                    if raw_subject:
                        decoded_list = decode_header(raw_subject)
                        for text, charset in decoded_list:
                            if isinstance(text, bytes):
                                subject += text.decode(charset or 'utf-8', errors='ignore')
                            else:
                                subject += str(text)

                    match = re.search(r'\b(\d{4})\b', subject)
                    if match:
                        otp = match.group(1)
                        mail.logout()
                        return otp, 4

        mail.logout()
    except Exception as e:
        print("Error:", e)
    
    return None, None

@app.route('/get-otp', methods=['GET'])
def get_otp():
    otp_code, otp_len = get_genius_otp()
    if otp_code:
        return jsonify({"status": "success", "otp": otp_code, "length": otp_len})
    else:
        return jsonify({"status": "waiting", "otp": None, "length": 0})

# ==========================================
# 2. نظام نتفليكس واستخراج رابط الموافقة
# ==========================================
@app.route('/get-netflix', methods=['GET'])
def get_netflix():
    raw_sub = request.args.get('sub_id')
    sub_id = clean_sub_id(raw_sub)
    
    if not sub_id or sub_id in ['null', 'undefined']:
        return jsonify({"status": "error", "message": "Subscription ID is required"}), 400
    
    blocked = load_json(BLOCKLIST_FILE)
    blocked_ids = [b.get("id") if isinstance(b, dict) else b for b in blocked]
    if sub_id in blocked_ids:
        return jsonify({
            "status": "blocked", 
            "message": "نعتذر، انتهت صلاحية هذا الرابط أو تم استبداله."
        }), 403
    
    mappings = load_json(MAPPING_FILE)
    backend_key = mappings.get(sub_id, sub_id)

    headers = {
        'accept': 'application/json',
        'referer': f'https://tv.ostories.me/?subscriptionId={backend_key}',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'x-api-key': backend_key
    }

    base_api = "https://tv.ostories.me/api/dealer-subscriptions"
    result = {
        "status": "success",
        "email": None,
        "signin_code": None,
        "temp_code": None,
        "login_code": None,
        "approval_url": None
    }

    with requests.Session() as s:
        s.headers.update(headers)
        
        # Sign-in Code
        try:
            r1 = s.get(f"{base_api}/signin-code", timeout=5)
            if r1.status_code == 200:
                d1 = r1.json()
                raw_c = d1.get("code") or d1.get("signInCode") or ""
                result["signin_code"] = raw_c
                result["email"] = d1.get("email") or d1.get("accountEmail")
                if "netflix.com" in str(raw_c):
                    result["approval_url"] = str(raw_c)
        except Exception:
            pass

        # Temp Code
        try:
            r2 = s.get(f"{base_api}/temp-code", timeout=5)
            if r2.status_code == 200:
                d2 = r2.json()
                result["temp_code"] = d2.get("code") or d2.get("tempCode")
                if not result["email"]:
                    result["email"] = d2.get("email")
        except Exception:
            pass

        # Login Verification Code
        try:
            r3 = s.get(f"{base_api}/login-verification-code", timeout=5)
            if r3.status_code == 200:
                d3 = r3.json()
                raw_v = d3.get("code") or d3.get("loginVerificationCode") or d3.get("verificationUrl") or d3.get("url") or ""
                result["login_code"] = raw_v
                if "netflix.com" in str(raw_v):
                    result["approval_url"] = str(raw_v)
                if not result["email"]:
                    result["email"] = d3.get("email")
        except Exception:
            pass

    # استخراج أي رابط موافقة يظهر في الردود
    for k in ["signin_code", "temp_code", "login_code"]:
        val = str(result.get(k) or "")
        match_url = re.search(r'(https?://[^\s]+)', val)
        if match_url:
            result["approval_url"] = match_url.group(1)

    return jsonify(result)

# ==========================================
# 3. لوحة الإدارة: التوليد التلقائي للرابط
# ==========================================
@app.route('/admin/list-blocked', methods=['POST'])
def admin_list_blocked():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401
    return jsonify({"status": "success", "blocked": load_json(BLOCKLIST_FILE)})

@app.route('/admin/revoke-and-issue', methods=['POST'])
def admin_revoke_and_issue():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "غير مصرح"}), 401
    
    current_sub = clean_sub_id(data.get("sub_id", ""))
    if not current_sub:
        return jsonify({"status": "error", "message": "يرجى وضع الرابط الحالي"}), 400

    mappings = load_json(MAPPING_FILE)
    original_target = mappings.get(current_sub, current_sub)

    # تعطيل الرابط الحالي
    blocked = load_json(BLOCKLIST_FILE)
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    blocked = [b for b in blocked if (b.get("id") if isinstance(b, dict) else b) != current_sub]
    blocked.insert(0, {"id": current_sub, "date": now_time})
    save_json(BLOCKLIST_FILE, blocked)

    # إنشاء معرف جديد وربطه
    new_sub_id = str(uuid.uuid4())
    mappings[new_sub_id] = original_target
    save_json(MAPPING_FILE, mappings)

    new_link = f"https://elevara1.shop/otp.netflix/?subscriptionId={new_sub_id}"

    return jsonify({
        "status": "success",
        "new_sub_id": new_sub_id,
        "new_link": new_link,
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
