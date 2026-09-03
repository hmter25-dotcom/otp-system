from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import re

app = Flask(__name__)
CORS(app)

# بيانات حسابك
EMAIL = "elevaraa76@outlook.com"
PASSWORD = "كلمة_المرور_الجديدة_هنا"
IMAP_SERVER = "imap-mail.outlook.com"

@app.route('/get-otp')
def get_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)
        mail.select('inbox')
        status, messages = mail.search(None, 'ALL')
        
        if not messages[0]:
            return jsonify({"status": "waiting", "message": "No emails found"})
            
        latest_email_id = messages[0].split()[-1]
        status, data = mail.fetch(latest_email_id, '(RFC822)')
        msg = email.message_from_bytes(data[0][1])
        
        email_content = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    email_content = part.get_payload(decode=True).decode()
        else:
            email_content = msg.get_payload(decode=True).decode()

        otp_match = re.search(r'\b\d{4,6}\b', email_content)
        if otp_match:
            return jsonify({"status": "success", "otp": otp_match.group(0)})
        else:
            return jsonify({"status": "waiting", "message": "Waiting for code..."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
