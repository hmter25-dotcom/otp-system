from flask import Blueprint, request, jsonify
import requests
import json
import re

canva_bp = Blueprint('canva_bp', __name__)

CANVA_COOKIES = (
    "CDI=f8ce3876-2448-47a8-9f7e-77a530665831; "
    "CAZ=cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyRrPJipyTvPCfc6aO1ItXYe_19f1XecOLuUXcrcHZsXXOj6Axg00lu1LYJFwtAKvYfg6OG2txDQC6Xy6Nc4OK_dshApmslTJWy4lQQNN6X98fYWeabM2HJHQwUig9-i-D3wNKRbMgPsdhLL4AGQDjgobdjNQqBHVcrJA-rXVzUVxbff6QUJsgzcT7HKm0udT7YDIeSF3diBALKyDEdX9CE6KZRUNIbGoxzgJuP4fkEwLDu4msUkrO_YDcEwXiCgUv-Z-4Q5h7pj1X4YqYNmoy8fw4bCbEcgm5zroCZz4gf7789a28; "
    "CB=BAGxMrCc42g; CAU=eyJBIjoiVUFHeE1uYy1TeUEiLCJCIjoiQkFHeE1yQ2M0MmcifQ==; "
    "CID=cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyTIK1azm4op5CehysNBUKa_vcRH7cR96mQKyaoow4rWWSawljNc8MRwqh3r146qg5QHjkLnbSqdsVF25npNLwzz8gRKzkzIwYD-7TQ03ce31d9; "
    "CUI=ZW4eAAAAA0roaKLUxFu3UINXQv6_hmbDQvBb0GX1UaHIqmz6nM8Fe2Cvij9LxxbYkE3uAM7nQyrAE6en6kKkMfM; CS=1"
)

def trigger_canva_invite(target_email):
    url = "https://www.canva.com/_ajax/invitation/brand/invitations/create"
    headers = {
        "accept": "*/*",
        "accept-language": "ar,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "cookie": CANVA_COOKIES,
        "origin": "https://www.canva.com",
        "referer": "https://www.canva.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "x-canva-brand": "BAGxMrCc42g",
        "x-canva-user": "UAGxMnc-SyA",
        "x-canva-request": "createbrandinvitations"
    }
    payload = {
        "K": "BAGxMrCc42g",
        "A?": "A",
        "A": [{"A": target_email, "B": "B"}],
        "B": True
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=8)
        return res.status_code == 200, res.text
    except Exception as e:
        return False, str(e)

# مسار الويب هوك الخاص بكانفا
@canva_bp.route('/webhook/canva', methods=['POST', 'GET'])
def canva_webhook():
    test_em = request.args.get('email')
    if test_em:
        success, info = trigger_canva_invite(test_em.strip())
        return jsonify({"status": "success" if success else "failed", "email": test_em, "details": info})

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
