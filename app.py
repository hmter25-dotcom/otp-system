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

# كلمة المرور السرية الخاصة بك لدخول لوحة التحكم
ADMIN_PASSWORD = "admin@elevaraa1451"

# ملف تخزين الاشتراكات المعطلة وملاحظاتها
BLOCKLIST_FILE = "blocked_subs.json"

def load_blocked():
    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r") as f:
                data = json.load(f)
                # دعم التوافقية لو كانت البيانات قديمة (قائمة نصوص عادية)
                if data and isinstance(data[0], str):
                    return [{"id": x, "note": "اشتراك عميل", "date": ""} for x in data]
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
    sub_id = request.args.get('sub_id')
    
    if not sub_id or sub_id in ['null', 'undefined']:
        return jsonify({"status": "error", "message": "Subscription ID is required"}), 400
    
    # فحص هل الاشتراك موجود في قائمة المحظورين؟
    blocked_subs = load_blocked()
    blocked_ids = [item["id"] for item in blocked_subs]
    
    if sub_id in blocked_ids:
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
# 3. دوال لوحة التحكم الإدارية الاحترافية
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
    
    raw_sub = data.get("sub_id", "").strip()
    note = data.get("note", "").strip() or "عميل بدون اسم"
    
    if not raw_sub:
        return jsonify({"status": "error", "message": "يرجى إدخال الرابط أو المعرف"}), 400
    
    # استخراج الـ ID بدقة لو أدخل المستخدم الرابط كاملاً
    match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', raw_sub, re.I)
    target_id = match.group(1) if match else raw_sub

    blocked = load_blocked()
    # التحقق هل هو معطل مسبقاً؟ لو موجود نحدث ملاحظته فقط
    existing = next((item for item in blocked if item["id"] == target_id), None)
    if existing:
        existing["note"] = note
    else:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
        blocked.append({"id": target_id, "note": note, "date": now_str})
        
    save_blocked(blocked)
    return jsonify({"status": "success", "message": "تم تعطيل الرابط بنجاح", "blocked": blocked})

@app.route('/admin/unblock', methods=['POST'])
def admin_unblock():
    data = request.get_json() or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401
    
    target_id = data.get("sub_id", "").strip()
    blocked = load_blocked()
    
    # حذف المعرف من قائمة الحظر لإعادة تفعيله فوراً
    blocked = [item for item in blocked if item["id"] != target_id]
    save_blocked(blocked)
    
    return jsonify({"status": "success", "message": "تمت إعادة تشغيل وتفعيل الرابط بنجاح", "blocked": blocked})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
