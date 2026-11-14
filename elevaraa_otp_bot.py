"""
Elevaraa Telegram bot: يراقب صناديق البريد ويرسل أكواد/روابط شاهد وكرانشي رول وديزني+ إلى تيليجرام.

متغيرات البيئة المطلوبة:
    TG_BOT_TOKEN            توكن البوت (إجباري)
    TG_ACTIVATION_PIN       رمز التفعيل (إجباري)
    DISNEY_EMAIL_ACCOUNT    الإيميل الرئيسي (إجباري)
    DISNEY_EMAIL_PASSWORD   App Password للإيميل الرئيسي (إجباري)

اختيارية:
    TG_ADMIN_ID             معرّف الأدمن (بدل التفعيل بالأمر)
    TG_WEBHOOK_SECRET       secret_token الذي تمرره لـ setWebhook (موصى به بشدة)
    MAIL_ENC_KEY            مفتاح Fernet لتشفير كلمات سر الإيميلات المحفوظة
    DATA_DIR                مجلد حفظ الملفات (الافتراضي: .)
    MAIL_MAX_AGE_SECONDS    تجاهل الرسائل الأقدم من هذه المدة (الافتراضي 1800)
    POLL_INTERVAL_SECONDS   فاصل الفحص (الافتراضي 15)
"""
import hmac
import html
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from imaplib import IMAP4_SSL

import requests
from flask import Blueprint, jsonify, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("elevaraa.telegram")
tg_bp = Blueprint("telegram_bot", __name__)


# ───────────────────────── الإعدادات ─────────────────────────
def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = _require_env("TG_BOT_TOKEN")
ACTIVATION_PIN = _require_env("TG_ACTIVATION_PIN")
MASTER_EMAIL = _require_env("DISNEY_EMAIL_ACCOUNT").lower()
MASTER_PASS = _require_env("DISNEY_EMAIL_PASSWORD")
WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "").strip()

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "bot_mail_accounts.json")
PROCESSED_FILE = os.path.join(DATA_DIR, "bot_processed_uids.json")

MAX_AGE_SECONDS = int(os.environ.get("MAIL_MAX_AGE_SECONDS", "1800"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
SCAN_LAST_N = 15
MAX_PROCESSED = 2000

IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "hotmail.com": "outlook.office365.com",
    "outlook.com": "outlook.office365.com",
    "outlook.sa": "outlook.office365.com",
    "live.com": "outlook.office365.com",
}

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
EMAIL_FIND_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IGNORED_LINK_WORDS = ("privacy", "unsubscribe", "help", "terms", "support")

if not WEBHOOK_SECRET:
    log.warning("TG_WEBHOOK_SECRET غير مضبوط: أي أحد يعرف رابط الويب هوك يستطيع إرسال طلبات مزيفة.")

# ───────────────────────── تشفير اختياري ─────────────────────────
_fernet = None
_enc_key = os.environ.get("MAIL_ENC_KEY", "").strip()
if _enc_key:
    try:
        from cryptography.fernet import Fernet, InvalidToken

        _fernet = Fernet(_enc_key.encode())
    except Exception as exc:
        raise RuntimeError(f"MAIL_ENC_KEY invalid or cryptography missing: {exc}")


def _encrypt(value):
    if not _fernet:
        return value
    return "enc:" + _fernet.encrypt(value.encode()).decode()


def _decrypt(value):
    if not value.startswith("enc:"):
        return value
    if not _fernet:
        raise RuntimeError("Encrypted password found but MAIL_ENC_KEY is not set")
    return _fernet.decrypt(value[4:].encode()).decode()


# ───────────────────────── التخزين (آمن للخيوط + كتابة ذرّية) ─────────────────────────
_file_lock = threading.RLock()
_check_lock = threading.Lock()


def _load_json(path, default):
    with _file_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as exc:
            log.error("Failed reading %s: %s", path, exc)
            return default


def _save_json(path, data):
    with _file_lock:
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception as exc:
            log.error("Failed saving %s: %s", path, exc)
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)


def get_admin_chat_id():
    env_id = os.environ.get("TG_ADMIN_ID", "").strip()
    if env_id:
        return env_id
    return str(_load_json(CONFIG_FILE, {}).get("admin_chat_id", "")).strip()


def set_admin_chat_id(chat_id):
    with _file_lock:
        cfg = _load_json(CONFIG_FILE, {})
        cfg["admin_chat_id"] = str(chat_id).strip()
        _save_json(CONFIG_FILE, cfg)


def get_all_mailboxes():
    boxes = [{"email": MASTER_EMAIL, "password": MASTER_PASS, "host": "imap.gmail.com"}]
    for item in _load_json(ACCOUNTS_FILE, []):
        try:
            if item.get("email", "").lower() == MASTER_EMAIL:
                continue
            boxes.append(
                {
                    "email": item["email"],
                    "password": _decrypt(item["password"]),
                    "host": item.get("host", "imap.gmail.com"),
                }
            )
        except Exception as exc:
            log.error("Skipping bad mailbox entry (%s): %s", item.get("email"), exc)
    return boxes


# ───────────────────────── تيليجرام ─────────────────────────
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504), allowed_methods=None)),
)


def _tg_api(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(2):
        try:
            resp = _session.post(url, json=payload, timeout=10)
            if resp.status_code == 429:
                wait = resp.json().get("parameters", {}).get("retry_after", 1)
                time.sleep(min(int(wait), 10))
                continue
            if not resp.ok:
                log.error("Telegram %s failed: %s %s", method, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except requests.RequestException as exc:
            log.error("Telegram %s network error: %s", method, type(exc).__name__)
            return None
    return None


def send_tg_message(chat_id, text, buttons=None, reply_keyboard=None):
    """يرجع True عند نجاح الإرسال."""
    if not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    elif reply_keyboard:
        payload["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True}
    return _tg_api("sendMessage", payload) is not None


def main_menu_keyboard():
    return [
        [{"text": "🔍 فحص الرسائل الآن"}, {"text": "📋 إيميلاتي المربوطة"}],
        [{"text": "➕ إضافة إيميلات"}, {"text": "📺 فتح منصة شاهد"}],
    ]


# ───────────────────────── تحليل الرسائل ─────────────────────────
def _decode_str(raw):
    parts = []
    try:
        for text, charset in decode_header(raw or ""):
            if isinstance(text, bytes):
                try:
                    parts.append(text.decode(charset or "utf-8", errors="replace"))
                except LookupError:
                    parts.append(text.decode("utf-8", errors="replace"))
            else:
                parts.append(str(text))
    except Exception:
        return str(raw or "")
    return "".join(parts)


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg):
    body_html, body_text = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/html":
            body_html.append(_decode_part(part))
        elif ctype == "text/plain":
            body_text.append(_decode_part(part))
    return "\n".join(body_html), "\n".join(body_text)


def _html_to_text(html_content):
    no_tags = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_content)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_tags)
    return html.unescape(no_tags)


def _extract_recipient(msg, fallback_email):
    for header in ("To", "Delivered-To", "X-Forwarded-To", "X-Original-To"):
        for found in EMAIL_FIND_RE.findall(_decode_str(msg.get(header, ""))):
            if found.lower() != MASTER_EMAIL:
                return found.lower()
    return fallback_email.lower()


def _extract_links(html_content, text_content):
    raw = re.findall(r'href=["\'](https?://[^"\'\s>]+)["\']', html_content, re.I)
    if not raw:
        raw = re.findall(r"(https?://[^\s<>\"']+)", text_content)
    seen, links = set(), []
    for link in raw:
        link = html.unescape(link)
        if link.lower().startswith("https://") and link not in seen:
            seen.add(link)
            links.append(link)
    return links


def _pick_link(links, keywords):
    clean = [l for l in links if not any(w in l.lower() for w in IGNORED_LINK_WORDS)]
    for link in clean:
        if any(k in link.lower() for k in keywords):
            return link
    return None


def _esc(value):
    return html.escape(str(value), quote=False)


def parse_service_email(msg, mailbox_email):
    sender = _decode_str(msg.get("From", "")).lower()
    subject = _decode_str(msg.get("Subject", ""))
    subject_lower = subject.lower()
    body_html, body_text = _extract_body(msg)
    plain = body_text or _html_to_text(body_html)
    full_lower = f"{body_html}\n{body_text}".lower()
    target = _esc(_extract_recipient(msg, mailbox_email))
    links = _extract_links(body_html, body_text)

    # 1) شاهد
    if "shahid" in sender or "شاهد" in subject_lower or "shahid.mbc.net" in full_lower:
        link = _pick_link(links, ["reset", "password", "forgot", "token", "login", "shahid.mbc.net"])
        if link:
            text = f"📺 <b>رابط إعادة تعيين كلمة مرور شاهد وصل!</b>\n\n📧 <b>الحساب:</b>\n<code>{target}</code>"
            buttons = [
                [{"text": "🔑 تعيين كلمة السر", "url": link}],
                [{"text": "🔗 فتح منصة شاهد", "url": "https://shahid.mbc.net/ar"}],
            ]
            return text, buttons

    # 2) كرانشي رول
    if "crunchyroll" in sender or "crunchyroll" in subject_lower:
        link = _pick_link(links, ["reset", "password", "verify", "confirm", "login", "auth", "crunchyroll"])
        if link:
            is_reset = any(w in subject_lower or w in full_lower for w in ("reset", "password", "كلمة المرور", "كلمة السر"))
            if is_reset:
                text = f"🟠 <b>رابط إعادة تعيين كلمة مرور Crunchyroll وصل!</b>\n\n📧 <b>الحساب:</b>\n<code>{target}</code>"
                buttons = [[{"text": "🔑 تعيين كلمة السر", "url": link}]]
            else:
                text = (
                    f"🟠 <b>رابط تأكيد الدخول Crunchyroll وصل!</b>\n\n📧 <b>الحساب:</b>\n<code>{target}</code>\n"
                    "⏳ اضغط الزر خلال 10 دقائق ليكتمل تسجيل الدخول"
                )
                buttons = [[{"text": "✅ تأكيد الدخول", "url": link}]]
            return text, buttons

    # 3) ديزني+
    if "disney" in sender or "disney" in subject_lower:
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", plain)
        if match:
            text = (
                f"🏰 <b>كود دخول Disney+ وصل!</b>\n\n📧 <b>الحساب:</b>\n<code>{target}</code>\n\n"
                f"🔢 <b>رمز المرور (اضغط للنسخ):</b>\n<code>{match.group(1)}</code>"
            )
            return text, None

    return None, None


def _is_too_old(msg):
    try:
        sent = parsedate_to_datetime(msg.get("Date", ""))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - sent).total_seconds() > MAX_AGE_SECONDS
    except Exception:
        return False


# ───────────────────────── فحص البريد ─────────────────────────
def check_all_inboxes(manual_chat_id=None):
    admin_id = manual_chat_id or get_admin_chat_id()
    if not admin_id:
        return 0

    if not _check_lock.acquire(timeout=60):
        log.warning("Inbox check skipped: another check is still running")
        return 0
    try:
        processed_list = _load_json(PROCESSED_FILE, [])
        processed = set(processed_list)
        new_found = 0

        for box in get_all_mailboxes():
            mail = None
            try:
                mail = IMAP4_SSL(box.get("host", "imap.gmail.com"), 993, timeout=30)
                mail.login(box["email"], box["password"])
                mail.select("INBOX", readonly=True)

                _, uv = mail.response("UIDVALIDITY")
                validity = uv[0].decode() if uv and uv[0] else "0"

                status, data = mail.uid("SEARCH", None, "ALL")
                if status != "OK" or not data or not data[0]:
                    continue

                for uid in reversed(data[0].split()[-SCAN_LAST_N:]):
                    key = f"{box['email']}:{validity}:{uid.decode()}"
                    if key in processed:
                        continue

                    status, msg_data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue

                    delivered = True
                    for part in msg_data:
                        if not isinstance(part, tuple):
                            continue
                        msg = message_from_bytes(part[1])
                        if _is_too_old(msg):
                            continue
                        text, buttons = parse_service_email(msg, box["email"])
                        if text:
                            if send_tg_message(admin_id, text, buttons=buttons):
                                new_found += 1
                            else:
                                delivered = False

                    if delivered:
                        processed.add(key)
                        processed_list.append(key)

            except Exception as exc:
                log.warning("IMAP check failed for %s: %s", box.get("email"), type(exc).__name__)
            finally:
                if mail is not None:
                    try:
                        mail.logout()
                    except Exception:
                        pass

        _save_json(PROCESSED_FILE, processed_list[-MAX_PROCESSED:])
        return new_found
    finally:
        _check_lock.release()


def _test_imap_login(email_addr, password, host):
    mail = None
    try:
        mail = IMAP4_SSL(host, 993, timeout=15)
        mail.login(email_addr, password)
        return True
    except Exception:
        return False
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


# ───────────────────────── أوامر البوت ─────────────────────────
_failed_activations = {}


def _activation_blocked(chat_id):
    count, first = _failed_activations.get(chat_id, (0, time.time()))
    if time.time() - first > 900:
        _failed_activations.pop(chat_id, None)
        return False
    return count >= 5


def _record_failed_activation(chat_id):
    count, first = _failed_activations.get(chat_id, (0, time.time()))
    _failed_activations[chat_id] = (count + 1, first)


def _handle_manual_check(chat_id):
    send_tg_message(chat_id, "⏳ <b>جاري فحص البريد الآن...</b>")
    if check_all_inboxes(manual_chat_id=chat_id) == 0:
        send_tg_message(chat_id, "📭 لا توجد رسائل جديدة حالياً.")


def _handle_add_accounts(chat_id, message_id, text):
    if message_id:
        _tg_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    with _file_lock:
        saved = _load_json(ACCOUNTS_FILE, [])
        added, failed = [], []
        for line in text[4:].strip().splitlines():
            if ":" not in line:
                continue
            em, pw = line.strip().split(":", 1)
            em, pw = em.strip().lower(), pw.strip().replace(" ", "")
            if not EMAIL_RE.match(em) or not pw:
                failed.append(em or "?")
                continue
            host = IMAP_HOSTS.get(em.split("@")[-1], "imap.gmail.com")
            if not _test_imap_login(em, pw, host):
                failed.append(em)
                continue
            saved = [x for x in saved if x.get("email") != em]
            saved.append({"email": em, "password": _encrypt(pw), "host": host})
            added.append(em)
        _save_json(ACCOUNTS_FILE, saved)

    msg = f"✅ تم حفظ <b>{len(added)}</b> إيميل بنجاح!"
    if failed:
        msg += "\n\n⚠️ فشل (صيغة خاطئة أو بيانات دخول غير صحيحة):\n" + "\n".join(
            f"• <code>{_esc(e)}</code>" for e in failed
        )
    msg += "\n\n🗑 حذفت رسالتك التي تحتوي كلمات السر."
    send_tg_message(chat_id, msg)


@tg_bp.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403

    data = request.get_json(silent=True) or {}
    message = data.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    admin_id = get_admin_chat_id()

    if text.startswith("/activate"):
        if admin_id and chat_id != admin_id:
            send_tg_message(chat_id, "🔒 البوت مرتبط بحساب آخر.")
            return jsonify({"ok": True})
        if _activation_blocked(chat_id):
            send_tg_message(chat_id, "⛔ محاولات كثيرة. حاول بعد 15 دقيقة.")
            return jsonify({"ok": True})
        supplied = text[len("/activate"):].strip()
        if hmac.compare_digest(supplied, ACTIVATION_PIN):
            _failed_activations.pop(chat_id, None)
            set_admin_chat_id(chat_id)
            send_tg_message(
                chat_id,
                "✅ <b>تم تفعيل البوت وربطه بحسابك بنجاح!</b>\n\nسيصلك الآن أي كود أو رابط من "
                "<b>شاهد</b> أو <b>كرانشي رول</b> أو <b>ديزني+</b> تلقائياً.",
                reply_keyboard=main_menu_keyboard(),
            )
        else:
            _record_failed_activation(chat_id)
            send_tg_message(chat_id, "❌ رمز التفعيل غير صحيح.")
        return jsonify({"ok": True})

    if not admin_id or chat_id != admin_id:
        send_tg_message(chat_id, "🔒 البوت خاص. أرسل أمر التفعيل أولاً.")
        return jsonify({"ok": True})

    if text == "/start":
        send_tg_message(
            chat_id,
            "👋 <b>أهلاً بك في بوت إليفارا (Elevaraa Bot)</b>\n\nاختر من القائمة بالأسفل:",
            reply_keyboard=main_menu_keyboard(),
        )

    elif text == "🔍 فحص الرسائل الآن":
        threading.Thread(target=_handle_manual_check, args=(chat_id,), daemon=True).start()

    elif text == "📋 إيميلاتي المربوطة":
        lines = [f"{i + 1}. <code>{_esc(b['email'])}</code>" for i, b in enumerate(get_all_mailboxes())]
        send_tg_message(chat_id, "📋 <b>الإيميلات المربوطة:</b>\n\n" + "\n".join(lines))

    elif text == "➕ إضافة إيميلات":
        send_tg_message(
            chat_id,
            "أرسل الإيميلات بهذا الشكل (كل إيميل في سطر):\n\n<code>add email@gmail.com:app_password</code>",
        )

    elif text.lower().startswith("add "):
        threading.Thread(target=_handle_add_accounts, args=(chat_id, message_id, text), daemon=True).start()

    elif text == "📺 فتح منصة شاهد":
        send_tg_message(
            chat_id,
            "🔗 افتح شاهد واطلب استعادة كلمة المرور، وسيصلك الرابط هنا:",
            buttons=[[{"text": "🔗 فتح منصة شاهد", "url": "https://shahid.mbc.net/ar"}]],
        )

    return jsonify({"ok": True})


# ───────────────────────── المراقب في الخلفية ─────────────────────────
_stop_event = threading.Event()
_watcher_started = False
_watcher_guard = threading.Lock()


def _background_watcher():
    backoff = POLL_INTERVAL
    while not _stop_event.is_set():
        try:
            if get_admin_chat_id():
                check_all_inboxes()
            backoff = POLL_INTERVAL
        except Exception:
            log.exception("loop error")
            backoff = min(backoff * 2, 300)
        _stop_event.wait(backoff)


def start_telegram_watcher():
    global _watcher_started
    with _watcher_guard:
        if _watcher_started:
            return
        _watcher_started = True
        threading.Thread(target=_background_watcher, name="tg-watcher", daemon=True).start()


def stop_telegram_watcher():
    _stop_event.set()
