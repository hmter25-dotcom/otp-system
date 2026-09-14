from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import re

app = Flask(__name__)
CORS(app)

IMAP_SERVER = "imap.gmail.com"
EMAIL_ACCOUNT = "elevaraa8@gmail.com"
EMAIL_PASSWORD = "zcfuvmpgibqatcer"

def fetch_latest_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("INBOX")

        # البحث فقط عن الرسائل الواردة من OSN
        status, messages = mail.search(None, '(FROM "OSN")')
        if status != "OK" or not messages[0]:
            # محاولة بحث عامة إذا لم يجد مرسل باسم OSN مباشرة
            status, messages = mail.search(None, "ALL")
            if status != "OK":
                return None, None

        email_ids = messages[0].split()
        if not email_ids:
            return None, None

        # فحص أحدث الرسائل الخاصة بـ OSN
        for email_id in reversed(email_ids[-3:]):
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject = ""
                    if msg["Subject"]:
                        subject = msg["Subject"]

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

                    full_text = subject + " " + body

                    # البحث عن نمط رسالة OSN الصريح (مثل: 5049 هو الرمز الخاص بك أو OTP)
                    match = re.search(r'\b(\d{4})\b', subject)
                    if match:
                        mail.logout()
                        return match.group(1), 4

                    match_body = re.search(r'(?:OTP|رمز|الرمز)[:\s]*(\d{4})', full_text, re.IGNORECASE)
                    if match_body:
                        mail.logout()
                        return match_body.group(1), 4

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
