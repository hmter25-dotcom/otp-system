from flask import Blueprint, request, jsonify
import requests
import json
import re

canva_bp = Blueprint('canva_bp', __name__)

def trigger_canva_invite(target_email):
    # استخدام مسار الدفعة المباشر
    url = "https://www.canva.com/_ajax/ae/v2/createBatch"
    
    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'ar,en-US;q=0.9,en;q=0.8',
        'content-type': 'application/json',
        'origin': 'https://www.canva.com',
        'referer': 'https://www.canva.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'x-canva-brand': 'BAGxMrCc42g',
        'x-canva-user': 'UAGxMnc-SyA'
    }

    raw_cookies = (
        "CDI=f8ce3876-2448-47a8-9f7e-77a530665831; "
        "CB=BAGxMrCc42g; "
        "CAU=eyJBIjoiVUFHeE1uYy1TeUEiLCJCIjoiQkFHeE1yQ2M0MmcifQ==; "
        "CID=cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyTIK1azm4op5CehysNBUKa_vcRH7cR96mQKyaoow4rWWSawljNc8MRwqh3r146qg5QHjkLnbSqdsVF25npNLwzz8gRKzkzIwYD-7TQ03ce31d9; "
        "CUI=ZW4eAAAAA0roaKLUxFu3UINXQv6_hmbDQvBb0GX1UaHIqmz6nM8Fe2Cvij9LxxbYkE3uAM7nQyrAE6en6kKkMfM; "
        "CS=1"
    )

    session = requests.Session()
    session.headers.update(headers)
    session.headers['cookie'] = raw_cookies

    payload = {
        "brandId": "BAGxMrCc42g",
        "invitations": [
            {
                "email": target_email,
                "role": "MEMBER"
            }
        ]
    }

    try:
        res = session.post(url, json=payload, timeout=10)
        return res.status_code, res.text
    except Exception as e:
        return 500, str(e)

@canva_bp.route('/webhook/canva', methods=['POST', 'GET'])
def canva_webhook():
    test_em = request.args.get('email')
    if test_em:
        code, info = trigger_canva_invite(test_em.strip())
        return jsonify({
            "status_code": code,
            "success": code in [200, 204],
            "email": test_em,
            "response": info[:250]
        })

    data = request.get_json(silent=True) or {}
    data_str = json.dumps(data)

    found_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', data_str)
    customer_email = None
    for em in found_emails:
        if not any(d in em.lower() for d in ["salla", "zid", "canva", "outlook", "elevaraa", "admin"]):
            customer_email = em.strip()
            break

    if not customer_email and found_emails:
        customer_email = found_emails[0].strip()

    if not customer_email:
        return jsonify({"status": "error", "message": "لم يتم العثور على بريد العميل في الطلب"}), 400

    code, info = trigger_canva_invite(customer_email)
    if code in [200, 204]:
        return jsonify({"status": "success", "message": f"تم إرسال الدعوة إلى {customer_email}"}), 200
    else:
        return jsonify({"status": "failed", "status_code": code, "details": info[:200]}), 500
