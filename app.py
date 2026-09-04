from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import re
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

EMAIL = "hmter25@gmail.com"
PASSWORD = "slevzjaclxtswqla"
IMAP_SERVER = "imap.gmail.com"

def get_text_from_email(msg):
    text_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    text_content += part.get_payload(decode=True).decode()
                except:
                    pass
            elif content_type == "text/html":
                try:
                    html = part.get_payload(decode=True).decode()
                    soup = BeautifulSoup(html, "html.parser")
                    text_content += soup.get_text(separator=' ')
                except:
                    pass
    else:
        content_type = msg.get_content_type()
        if content_type == "text/plain":
             try:
                 text_content = msg.get_payload(decode=True).decode()
             except:
                 pass
        elif content_type == "text/html":
             try:
                 html = msg.get_payload(decode=True).decode()
                 soup = BeautifulSoup(html, "html.parser")
                 text_content = soup.get_text(separator=' ')
             except:
                 pass
    return text_content

@app.route('/get-otp')
def get_otp():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)
        mail.select('inbox')
        
        status, messages = mail.search(None, 'ALL')
        
        if not messages[0]:
            return jsonify({"status": "waiting", "message": "لا توجد رسائل"})
            
        email_ids = messages[0].split()[-5:]
        email_ids.reverse()
        
        for e_id in email_ids:
            status, data = mail.fetch(e_id, '(RFC822)')
            msg = email.message_from_bytes(data[0][1])
            
            email_content = get_text_from_email(msg)
            
            otp_match = re.search(r'\b\d{4,6}\b', email_content)
            if otp_match:
                return jsonify({"status": "success", "otp": otp_match.group(0)})
                
        return jsonify({"status": "waiting", "message": "جاري انتظار الكود..."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
