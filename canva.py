from flask import Blueprint, request, jsonify
import requests
import json
import re

canva_bp = Blueprint('canva_bp', __name__)

def trigger_canva_invite(target_email):
    url = "https://www.canva.com/_ajax/invitation/brand/invitations/create"
    
    headers = {
        'accept': '*/*',
        'accept-language': 'ar,en-US;q=0.9,en;q=0.8',
        'content-type': 'application/json;charset=UTF-8',
        'origin': 'https://www.canva.com',
        'priority': 'u=1, i',
        'referer': 'https://www.canva.com/',
        'sec-ch-ua': '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"13.0.0"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
        'x-canva-accept-prefix': 'no-prefix',
        'x-canva-active-user': 'eyJBIjoiVUFHeE1uYy1TeUEiLCJCIjoiQkFHeE1yQ2M0MmcifQ==',
        'x-canva-app': 'home',
        'x-canva-brand': 'BAGxMrCc42g',
        'x-canva-locale': 'ar',
        'x-canva-request': 'createbrandinvitations',
        'x-canva-user': 'UAGxMnc-SyA'
    }

    # تجميع الكوكيز وتمريرها في الجلسة
    p1 = "CDI=f8ce3876-2448-47a8-9f7e-77a530665831; "
    p2 = "CB=BAGxMrCc42g; "
    p3 = "CAU=eyJBIjoiVUFHeE1uYy1TeUEiLCJCIjoiQkFHeE1yQ2M0MmcifQ==; "
    p4 = "CID=cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyTIK1azm4op5CehysNBUKa_vcRH7cR96mQKyaoow4rWWSawljNc8MRwqh3r146qg5QHjkLnbSqdsVF25npNLwzz8gRKzkzIwYD-7TQ03ce31d9; "
    p5 = "CUI=ZW4eAAAAA0roaKLUxFu3UINXQv6_hmbDQvBb0GX1UaHIqmz6nM8Fe2Cvij9LxxbYkE3uAM7nQyrAE6en6kKkMfM; "
    p6 = "CS=1"
    
    session = requests.Session()
    session.headers.update(headers)
    session.headers['cookie'] = p1 + p2 + p3 + p4 + p5 + p6

    payload = {
        "K": "BAGxMrCc42g",
        "A?": "A",
        "A": [{"A": target_email, "B": "B"}],
        "B": True
    }

    try:
        res = session.post(url, json=payload, timeout=12)
        # التحقق من أن الاستجابة ليست صفحة فحص HTML
        is_success = (res.status_code == 200) and ("<!DOCTYPE html>" not in res.text)
        return is_success, res.text
    except Exception as e:
        return False, str(e)

# مسار الـ Webhook الخاص بكانفا
@canva_bp.route('/webhook/canva', methods=['POST', 'GET'])
def canva_webhook():
    test_em = request.args.get('email')
    if test_em:
        success, info = trigger_canva_invite(test_em.strip())
        return jsonify({
            "status": "success" if success else "failed",
            "email": test_em,
            "response": info
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

    success, info = trigger_canva_invite(customer_email)
    if success:
        return jsonify({"status": "success", "message": f"تم إرسال دعوة كانفا للعميل: {customer_email}"}), 200
    else:
        return jsonify({"status": "failed", "message": "فشل إرسال الدعوة", "details": info}), 500
