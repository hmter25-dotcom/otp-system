from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import re
from bs4 import BeautifulSoup
from collections import Counter
from email.header import decode_header

app = Flask(__name__)
CORS(app)

EMAIL = "hmter25@gmail.com"
PASSWORD = "slevzjaclxtswqla"
IMAP_SERVER = "imap.gmail.com"

def get_text_from_email(msg):
    text_content = ""
    
    # 1. قراءة الكود من عنوان الإيميل (Subject) بشكل آمن
    try:
        subject = msg.get("Subject", "")
        if subject:
            for p, enc in decode_header(subject):
                if isinstance(p, bytes):
                    # استخدام errors='ignore' لتخطي أي مشكلة في الحروف العربية
                    text_content += p.decode(enc if enc else 'utf-8', errors='ignore') + " "
                else:
                    text_content += str(p) + " "
    except:
        pass

    # 2. قراءة الكود من داخل الإيميل بشكل آمن
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ["text/plain", "text/html"]:
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        decoded_payload = payload.decode('utf-8', errors='ignore')
                        if content_type == "text/html":
                            soup = BeautifulSoup(decoded_payload, "html.parser")
                            text_content += " " + soup.get_text(separator=' ')
                        else:
                            text_content += " " + decoded_payload
                except:
                    pass
    else:
        content_type = msg.get_content_type()
        if content_type in ["text/plain", "text/html"]:
             try:
                 payload = msg.get_payload(decode=True)
                 if payload:
                     decoded_payload = payload.decode('utf-8', errors='ignore')
                     if content_type == "text/html":
                         soup = BeautifulSoup(decoded_payload, "html.parser")
                         text_content += " " + soup.get_text(separator=' ')
                     else:
                         text_content += " " + decoded_payload
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
            
            otps = re.findall(r'\b\d{4,6}\b', email_content)
            
            # استبعاد التواريخ
            ignore_list = ['2024', '2025', '2026', '2027', '1446', '1447', '1448', '1449']
            valid_otps = [num for num in otps if num not in ignore_list]
            
            if valid_otps:
                # أخذ الرقم الأكثر تكراراً لضمان أنه الكود الصحيح
                best_otp = Counter(valid_otps).most_common(1)[0][0]
                return jsonify({"status": "success", "otp": best_otp})
                
        return jsonify({"status": "waiting", "message": "جاري انتظار الكود..."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
