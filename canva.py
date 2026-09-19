from flask import Blueprint, request, jsonify
from playwright.sync_api import sync_playwright
import json
import re

canva_bp = Blueprint('canva_bp', __name__)

COOKIES_DATA = [
    {"name": "CDI", "value": "f8ce3876-2448-47a8-9f7e-77a530665831", "domain": ".canva.com", "path": "/"},
    {"name": "CAZ", "value": "cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyRrPJipyTvPCfc6aO1ItXYe_19f1XecOLuUXcrcHZsXXOj6Axg00lu1LYJFwtAKvYfg6OG2txDQC6Xy6Nc4OK_dshApmslTJWy4lQQNN6X98fYWeabM2HJHQwUig9-i-D3wNKRbMgPsdhLL4AGQDjgobdjNQqBHVcrJA-rXVzUVxbff6QUJsgzcT7HKm0udT7YDIeSF3diBALKyDEdX9CE6KZRUNIbGoxzgJuP4fkEwLDu4msUkrO_YDcEwXiCgUv-Z-4Q5h7pj1X4YqYNmoy8fw4bCbEcgm5zroCZz4gf7789a28", "domain": ".canva.com", "path": "/"},
    {"name": "CB", "value": "BAGxMrCc42g", "domain": ".canva.com", "path": "/"},
    {"name": "CAU", "value": "eyJBIjoiVUFHeE1uYy1TeUEiLCJCIjoiQkFHeE1yQ2M0MmcifQ==", "domain": ".canva.com", "path": "/"},
    {"name": "CID", "value": "cnvanZW4eAAAAA9BkFtdArDvNTyRFeoMSEyTIK1azm4op5CehysNBUKa_vcRH7cR96mQKyaoow4rWWSawljNc8MRwqh3r146qg5QHjkLnbSqdsVF25npNLwzz8gRKzkzIwYD-7TQ03ce31d9", "domain": ".canva.com", "path": "/"},
    {"name": "CUI", "value": "ZW4eAAAAA0roaKLUxFu3UINXQv6_hmbDQvBb0GX1UaHIqmz6nM8Fe2Cvij9LxxbYkE3uAM7nQyrAE6en6kKkMfM", "domain": ".canva.com", "path": "/"},
    {"name": "CS", "value": "1", "domain": ".canva.com", "path": "/"}
]

def run_browser_invite(target_email):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu"
                ]
            )
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar",
                viewport={"width": 1280, "height": 720}
            )
            context.add_cookies(COOKIES_DATA)
            page = context.new_page()

            # فتح كانفا لتثبيت الجلسة
            page.goto("https://www.canva.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            # تنفيذ الإرسال من داخل بيئة المتصفح الموثوقة
            js_script = f"""
            async () => {{
                try {{
                    const res = await fetch('https://www.canva.com/_ajax/invitation/brand/invitations/create', {{
                        method: 'POST',
                        headers: {{
                            'content-type': 'application/json;charset=UTF-8',
                            'x-canva-brand': 'BAGxMrCc42g',
                            'x-canva-user': 'UAGxMnc-SyA',
                            'x-canva-request': 'createbrandinvitations'
                        }},
                        body: JSON.stringify({{
                            "K": "BAGxMrCc42g",
                            "A?": "A",
                            "A": [{{"A": "{target_email}", "B": "B"}}],
                            "B": true
                        }})
                    }});
                    const txt = await res.text();
                    return {{ status: res.status, body: txt }};
                }} catch (e) {{
                    return {{ status: 500, body: e.toString() }};
                }}
            }}
            """
            result = page.evaluate(js_script)
            browser.close()
            return result.get("status", 500), result.get("body", "")
    except Exception as e:
        return 500, str(e)

@canva_bp.route('/webhook/canva', methods=['POST', 'GET'])
def canva_webhook():
    test_em = request.args.get('email')
    if test_em:
        code, body = run_browser_invite(test_em.strip())
        return jsonify({
            "status_code": code,
            "success": (code == 200),
            "email": test_em,
            "response": body[:200]
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

    code, body = run_browser_invite(customer_email)
    if code == 200:
        return jsonify({"status": "success", "message": f"تم إرسال الدعوة إلى {customer_email}"}), 200
    else:
        return jsonify({"status": "failed", "status_code": code, "details": body[:200]}), 500
