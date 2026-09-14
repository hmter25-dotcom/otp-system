from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import re

app = Flask(__name__)
CORS(app)

# إعدادات جيميل الرسمية
IMAP_SERVER = "imap.gmail.com"
EMAIL_ACCOUNT = "elevaraa8@gmail.com"
EMAIL_PASSWORD = "zcfuvmpgibqatcer"  # كلمة مرور التطبيق بدون مسافات

def fetch_latest_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")

        # البحث عن آخر الرسائل
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return None, None

        email_ids = messages[0].split()
        if not email_ids:
            return None, None

        # فحص أحدث 3 رسائل للبحث عن الرمز
        for email_id in reversed(email_ids[-3:]):
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type in ["text/plain", "text/html"]:
                                try:
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body += payload.decode('utf-8', errors='ignore')
                                except:
                                    pass
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='ignore')

                    # البحث عن أول 4 أرقام متتالية في الرسالة (رمز التحقق)
                    match_4 = re.search(r'\b\d{4}\b', body)
                    if match_4:
                        mail.logout()
                        return match_4.group(0), 4

        mail.logout()
    except Exception as e:
        print("Error:", e)
    
    return None, None

@app.route('/get-otp', methods=['GET'])
def get_otp():
    otp_code, otp_len = fetch_latest_otp()
    if otp_code:
        return jsonify({"status": "success", "otp": otp_code, "length": otp_len})
    else:
        return jsonify({"status": "waiting", "otp": None, "length": 0})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
