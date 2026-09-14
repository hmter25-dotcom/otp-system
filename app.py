from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
from email.header import decode_header
import re

app = Flask(__name__)
CORS(app)

IMAP_SERVER = "imap.gmail.com"
EMAIL_ACCOUNT = "elevaraa8@gmail.com"
EMAIL_PASSWORD = "zcfuvmpgibqatcer"

def get_genius_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("INBOX")

        # جلب كل الرسائل
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return None, None

        email_ids = messages[0].split()
        if not email_ids:
            return None, None

        # فحص أحدث 15 رسالة (من الأجدد للأقدم)
        for e_id in reversed(email_ids[-15:]):
            # الذكاء هنا: جلب "رأس الرسالة" فقط (المرسل والعنوان) وتجاهل المحتوى الداخلي تماماً!
            status, msg_data = mail.fetch(e_id, '(BODY.PEEK[HEADER])')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])

                    # 1. فلترة صارمة: هل المرسل هو OSN؟
                    sender = str(msg.get("From", "")).lower()
                    if "osn" not in sender:
                        continue # إذا لم يكن OSN، تخطى الرسالة فوراً

                    # 2. فك تشفير عنوان الرسالة
                    subject = ""
                    raw_subject = msg.get("Subject", "")
                    if raw_subject:
                        decoded_list = decode_header(raw_subject)
                        for text, charset in decoded_list:
                            if isinstance(text, bytes):
                                subject += text.decode(charset or 'utf-8', errors='ignore')
                            else:
                                subject += str(text)

                    # 3. سحب أول 4 أرقام من العنوان مباشرة (مثل: 8512 هو الرمز الخاص بك)
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
