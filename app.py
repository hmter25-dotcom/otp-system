from flask import Flask, jsonify, request
from flask_cors import CORS
import imaplib
import email
from email.header import decode_header
import re
import requests
import json
import os
import datetime

app = Flask(__name__)
CORS(app)

ADMIN_PASSWORD = "admin@elevaraa1451"
BLOCKLIST_FILE = "blocked_subs.json"

def clean_sub_id(raw_str):
    if not raw_str:
        return ""
    # استخراج نمط المعرف بدقة سواء كان رابط كامل أو مقصوص أو ID فقط
    match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', raw_str, re.I)
    if match:
        return match.group(1).lower()
    return raw_str.strip()

def load_blocked():
    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r") as f:
                data = json.load(f)
                if data and isinstance(data[0], str):
                    return [{"id": x, "note": "اشتراك سابق", "date": ""} for x in data]
                return data
        except Exception:
            return []
    return []

def save_blocked(blocked_list):
    try:
        with open(BLOCKLIST_FILE, "w") as f:
            json.dump(blocked_list, f)
    except Exception as e:
        print("Error saving blocklist:", e)

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
# 2. نظام نتفليكس وفحص الحظر
# ==========================================
@app.route('/get-netflix', methods=['GET'])
def get_netflix():
    raw_sub = request.args.get('sub_id')
    sub_id = clean_sub_id(raw_sub)
    
    if not sub_id or sub_id in ['null', 'undefined']:
        return jsonify({"status": "error", "message": "Subscription ID is required"}), 400
    
    blocked_subs = load_blocked()
    blocked_ids = [item["id"].lower() for item in blocked_subs]
    
    if sub_id.lower() in blocked_ids:
        return jsonify({
            "status": "blocked", 
            "message": "نعتذر، تم إيقاف صلاحية هذا الاشتراك لانتهاء فترة الاستخدام."
        }), 403
    
    headers = {
        'accept': '*/*',
        'accept-language': 'ar,en-US;q=0.9,en;q=0.8',
        'content-type': 'application/json',
        'referer': f'https://tv.ostories.me/?subscriptionId={sub_id}',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'x-api-key': sub_id
    }

    base_api = "https://tv.ostories.me/api/dealer-subscriptions"
    
    result = {
        "status": "success",
        "email": None,
        "signin_code": None,
        "temp_code": None,
        "login_code": None
    }

    try:
        res_signin = requests.get(f"{base_api}/signin-code", headers=headers, timeout=10)
        if res_signin.status_code == 200:
            d = res_signin.json()
            result["signin_code"] = d.get("code") or d.get("signInCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))
            if "email" in d:
                result["email"] = d["email"]

        res_temp = requests.get(f"{base_api}/temp-code", headers=headers, timeout=10)
        if res_temp.status_code == 200:
            d = res_temp.json()
            result["temp_code"] = d.get("code") or d.get("tempCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))

        res_login = requests.get(f"{base_api}/login-verification-code", headers=headers, timeout=10)
        if res_login.status_code == 200:
            d = res_login.json()
            result["login_code"] = d.get("code") or d.get("loginVerificationCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))

        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ==========================================
# 3. لوحة الإدارة: الحظر، إلغاء الحظر، والتبديل
# ==========================================
@app.route('/admin/list-blocked', methods=['POST'])
def admin_list_blocked():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401
    return jsonify({"status": "success", "blocked": load_blocked()})

@app.route('/admin/block', methods=['POST'])
def admin_block():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401
    
    target_id = clean_sub_id(data.get("sub_id", ""))
    note = data.get("note", "").strip() or "اشتراك عميل"
    
    if not target_id:
        return jsonify({"status": "error", "message": "يرجى إدخال الرابط أو المعرف بشكل صحيح"}), 400

    blocked = load_blocked()
    existing = next((item for item in blocked if item["id"].lower() == target_id.lower()), None)
    if existing:
        existing["note"] = note
    else:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
        blocked.append({"id": target_id, "note": note, "date": now_str})
        
    save_blocked(blocked)
    return jsonify({"status": "success", "message": "تم تعطيل الرابط وحفظه بنجاح", "blocked": blocked})

@app.route('/admin/unblock', methods=['POST'])
def admin_unblock():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401
    
    target_id = clean_sub_id(data.get("sub_id", ""))
    blocked = load_blocked()
    blocked = [item for item in blocked if item["id"].lower() != target_id.lower()]
    save_blocked(blocked)
    
    return jsonify({"status": "success", "message": "تمت إعادة تفعيل الرابط بنجاح", "blocked": blocked})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
