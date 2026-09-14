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

        # جلب أحدث الرسائل لضمان السرعة والمرونة
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            mail.logout()
            return None, None

        email_ids = messages[0].split()
        if not email_ids:
            mail.logout()
            return None, None

        # فحص أحدث 15 رسالة من الأحدث للأقدم
        for email_id in reversed(email_ids[-15:]):
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    subject = ""
                    if msg["Subject"]:
                        try:
                            from email.header import decode_header
                            decoded = decode_header(msg["Subject"])
                            for text, encoding in decoded:
                                if isinstance(text, bytes):
                                    subject += text.decode(encoding or 'utf-8', errors='ignore')
                                else:
                                    subject += text
                        except:
                            subject = msg["Subject"]

                    from_addr = msg.get("From", "")
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ["text/plain", "text/html"]:
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

                    full_text = (subject + " " + body).lower()
                    
                    # التحقق بذكاء هل الرسالة خاصة بـ OSN فعلاً
                    if "osn" in from_addr.lower() or "osn" in subject.lower() or "osn+" in full_text:
                        # استخراج الرمز المكون من 4 أرقام حصرياً من رسالة OSN
                        matches = re.findall(r'\b\d{4}\b', full_text)
                        if matches:
                            otp = matches[0]
                            mail.logout()
                            return otp, 4

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
