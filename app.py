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
        mail.select("inbox")

        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return None, None

        email_ids = messages[0].split()
        if not email_ids:
            return None, None

        # فحص أحدث 3 رسائل
        for email_id in reversed(email_ids[-3:]):
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject = ""
                    if msg["Subject"]:
                        try:
                            # فك تشفير عنوان الرسالة إن وجد
                            from email.header import decode_header
                            decoded_headers = decode_header(msg["Subject"])
                            for text, encoding in decoded_headers:
                                if isinstance(text, bytes):
                                    subject += text.decode(encoding or 'utf-8', errors='ignore')
                                else:
                                    subject += text
                        except:
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

                    # 1. البحث أولاً في عنوان الرسالة (لأن OSN تحط الرمز في العنوان مثل: "5049 هو الرمز الخاص بك")
                    match_sub = re.search(r'\b(\d{4})\b', subject)
                    if match_sub:
                        mail.logout()
                        return match_sub.group(1), 4

                    # 2. البحث بجانب كلمات دالة في نص الرسالة مثل OTP أو رمز
                    match_body = re.search(r'(?:OTP|رمز|verification|code)[:\s]*(\d{4})', body, re.IGNORECASE)
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
