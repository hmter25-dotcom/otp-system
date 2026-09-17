from flask import Flask, jsonify, request
from flask_cors import CORS
import imaplib
import email
from email.header import decode_header
import re
import requests

app = Flask(__name__)
CORS(app)

# ==========================================
# 1. نظام OSN (يعمل بكفاءة دون تغيير)
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
# 2. نظام نتفليكس (سحب الأكواد بالـ API Key المباشر)
# ==========================================
@app.route('/get-netflix', methods=['GET'])
def get_netflix():
    sub_id = request.args.get('sub_id', 'f889333b-8af4-46fa-9154-b14f90c26137')
    
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
        "email": "zmediase@ostories.net",
        "signin_code": None,
        "temp_code": None,
        "login_code": None
    }

    try:
        # 1. سحب كود تسجيل الدخول (Sign-In)
        res_signin = requests.get(f"{base_api}/signin-code", headers=headers, timeout=10)
        if res_signin.status_code == 200:
            d = res_signin.json()
            result["signin_code"] = d.get("code") or d.get("signInCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))
            if "email" in d:
                result["email"] = d["email"]

        # 2. سحب الرمز المؤقت (Temporary)
        res_temp = requests.get(f"{base_api}/temp-code", headers=headers, timeout=10)
        if res_temp.status_code == 200:
            d = res_temp.json()
            result["temp_code"] = d.get("code") or d.get("tempCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))

        # 3. سحب كود التحقق (Login Verification)
        res_login = requests.get(f"{base_api}/login-verification-code", headers=headers, timeout=10)
        if res_login.status_code == 200:
            d = res_login.json()
            result["login_code"] = d.get("code") or d.get("loginVerificationCode") or (d.get("data", {}).get("code") if isinstance(d.get("data"), dict) else d.get("data"))

        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
