"""
Горячий След — payment middleware.

Two endpoints:
  GET  /create-payment    — called by Tilda button. Creates payment in GetPlatinum, redirects to checkout.
  POST /payment-callback  — called by GetPlatinum on successful payment. Issues Telegram invite + email.

All secrets via env vars (see .env.example).
"""
import hashlib
import json
import logging
import os
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, redirect, request
from werkzeug.middleware.proxy_fix import ProxyFix

# ─────────────────────────── config ───────────────────────────

GPL_API_KEY = os.environ["GPL_API_KEY"]
GPL_ACCOUNT = os.environ["GPL_ACCOUNT"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHANNEL_ID = int(os.environ["TG_CHANNEL_ID"])
UNISENDER_KEY = os.environ["UNISENDER_KEY"]
UNISENDER_LIST_ID = os.environ["UNISENDER_LIST_ID"]
UNISENDER_SENDER_NAME = os.environ["UNISENDER_SENDER_NAME"]
UNISENDER_SENDER_EMAIL = os.environ["UNISENDER_SENDER_EMAIL"]
SUCCESS_URL = os.environ["SUCCESS_URL"]
FAIL_URL = os.environ["FAIL_URL"]

PRODUCT_NAME = os.environ.get("PRODUCT_NAME", "Горячий След")
PRODUCT_PRICE = int(os.environ.get("PRODUCT_PRICE", "3"))  # TEMP: smoke-test price

# ─────────────────────────── app ──────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hotrack")

app = Flask(__name__)
# Trust X-Forwarded-* headers set by TimeWeb's reverse proxy so request.host_url
# returns the public https://... URL, not the internal one.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

_processed_lock = threading.Lock()
_processed: set[str] = set()


# ─────────────────────────── helpers ──────────────────────────

# Retry transient network failures (timeout / connection reset) with exponential backoff.
# We do NOT retry on 4xx/5xx — those mean the remote saw the request, retrying could double-charge / double-send.
_TRANSIENT_EXC = (requests.ConnectionError, requests.Timeout)


def _post_with_retry(url, *, attempts=3, base_delay=1.0, **kwargs):
    last_exc = None
    for i in range(attempts):
        try:
            return requests.post(url, **kwargs)
        except _TRANSIENT_EXC as exc:
            last_exc = exc
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                log.warning("POST %s transient error (%s), retry %d/%d in %.1fs", url, exc, i + 1, attempts - 1, delay)
                time.sleep(delay)
    raise last_exc


def _gpl_init_payment(server_base_url: str) -> str | None:
    """Call GetPlatinum init-payment-url. Returns formUrl or None."""
    deal_id = uuid.uuid4().hex
    client_id = uuid.uuid4().hex
    # GetPlatinum принимает суммы в копейках.
    amount_kopecks = PRODUCT_PRICE * 100
    payload = {
        "dealId": deal_id,
        "currency": "RUB",
        "amount": amount_kopecks,
        "positions": [
            {
                "prefix": 12,
                "name": PRODUCT_NAME,
                "price": amount_kopecks,
                "quantity": 1,
                "vat": "none",
            }
        ],
        "clientParams": {"clientId": client_id},
        "notificationUrl": f"{server_base_url.rstrip('/')}/payment-callback",
        "successUrl": SUCCESS_URL,
        "failUrl": FAIL_URL,
        "customParams": {"dealId": deal_id},
    }
    url = f"https://{GPL_ACCOUNT}.getplatinum.ru/api/public/pay/init-payment-url"
    headers = {
        "Authorization": f"Bearer {GPL_API_KEY}",
        "Content-Type": "application/json",
    }
    log.info("GPL init-payment-url request: %s", json.dumps(payload, ensure_ascii=False))
    resp = _post_with_retry(url, headers=headers, json=payload, timeout=10)
    log.info("GPL init-payment-url response: %d %s", resp.status_code, resp.text[:1000])
    resp.raise_for_status()
    data = resp.json()
    # Try several plausible shapes — exact format only confirmed once first call lands.
    return (
        data.get("formUrl")
        or data.get("paymentUrl")
        or data.get("url")
        or (data.get("result") or {}).get("formUrl")
        or (data.get("data") or {}).get("formUrl")
    )


def _tg_create_invite(order_id: str) -> str:
    """Create one-time Telegram invite link. Returns the link string."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createChatInviteLink"
    body = {
        "chat_id": TG_CHANNEL_ID,
        "member_limit": 1,
        "name": f"order-{order_id}"[:32],  # Telegram name limit
    }
    # Telegram API нестабилен с RU-IP (RKN-«рябь»). 5 попыток за ~30 секунд.
    resp = _post_with_retry(url, json=body, timeout=10, attempts=5, base_delay=1.0)
    log.info("Telegram createChatInviteLink: %d %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram returned error: {data}")
    return data["result"]["invite_link"]


def _us_send_email(to_email: str, invite_link: str) -> None:
    """Send transactional email via UniSender sendEmail."""
    html = _email_template(invite_link)
    resp = _post_with_retry(
        "https://api.unisender.com/ru/api/sendEmail",
        params={"format": "json"},
        data={
            "api_key": UNISENDER_KEY,
            "email": to_email,
            "sender_name": UNISENDER_SENDER_NAME,
            "sender_email": UNISENDER_SENDER_EMAIL,
            "subject": "Горячий След — ваш доступ готов",
            "body": html,
            "list_id": UNISENDER_LIST_ID,
        },
        timeout=15,
    )
    log.info("UniSender sendEmail: %d %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"UniSender error: {data}")


def _email_template(invite_link: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#ece6d8;font-family:'PT Serif',Georgia,serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#ece6d8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="max-width:600px;background:#FFFDF7;padding:32px;border:2px solid #B91C1C;">
        <tr><td>
          <h1 style="margin:0 0 16px 0;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:900;font-size:28px;line-height:1.1;color:#1A1A1A;text-align:center;">
            Горячий След — доступ готов
          </h1>
          <p style="margin:0 0 24px 0;font-size:17px;line-height:1.5;color:#1A1A1A;">
            Спасибо за покупку. Ниже — личная одноразовая ссылка для входа в закрытый Telegram-канал. Внутри ты сразу увидишь закреплённый PDF-протокол.
          </p>
          <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin:24px 0;">
            <tr><td align="center">
              <a href="{invite_link}" style="display:inline-block;background:#B91C1C;color:#FFFFFF;text-decoration:none;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:900;font-size:18px;padding:18px 36px;letter-spacing:0.5px;">
                ВОЙТИ В КАНАЛ
              </a>
            </td></tr>
          </table>
          <p style="margin:24px 0 8px 0;font-size:14px;color:#555;">Если кнопка не работает, скопируй ссылку:</p>
          <p style="margin:0 0 24px 0;font-size:13px;word-break:break-all;background:#FAF6EE;padding:10px 12px;border:1px solid #DDD;color:#333;font-family:Consolas,Monaco,monospace;">
            {invite_link}
          </p>
          <p style="margin:0 0 8px 0;font-size:14px;color:#555;">
            Ссылка одноразовая и работает только для тебя. Не делись ей.
          </p>
          <hr style="border:none;border-top:1px solid #DDD;margin:32px 0;">
          <p style="margin:0;font-size:12px;color:#999;text-align:center;">
            Если что-то не сработало — <a href="https://t.me/gumirovyaroslav" style="color:#999;">напиши нам в Telegram</a>.<br>
            — Братья Гумировы
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _extract_email(body: dict) -> str | None:
    """GPL puts email in clientInfo.email. Other keys are belt-and-suspenders for any future change."""
    return (
        (body.get("clientInfo") or {}).get("email")
        or body.get("email")
        or body.get("customerEmail")
        or body.get("client_email")
        or body.get("payerEmail")
        or (body.get("client") or {}).get("email")
        or (body.get("customer") or {}).get("email")
        or (body.get("payer") or {}).get("email")
    )


def _extract_order_id(body: dict, raw: str) -> str:
    """GPL primary identifier is dealId. mdOrder is GPL-internal, kept as fallback."""
    return str(
        body.get("dealId")
        or (body.get("paymentData") or {}).get("mdOrder")
        or body.get("id")
        or body.get("orderId")
        or body.get("paymentId")
        or body.get("order_id")
        or body.get("transactionId")
        or hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    )


def _is_successful(body: dict, raw_haystack: str) -> bool:
    """GPL signals success via isSuccess: true (boolean). Other shapes are kept as fallbacks."""
    if body.get("isSuccess") is True:
        return True
    if body.get("status") in {"success", "paid", "completed", "paymentStatusSuccess"}:
        return True
    if "paymentstatussuccess" in raw_haystack:
        return True
    return False


# ─────────────────────────── routes ───────────────────────────

@app.route("/create-payment", methods=["GET"])
def create_payment():
    # Лог конклюдентного согласия — IP, UA, время. Сшивается с email из callback по близкому таймстампу.
    log.info(
        "CONSENT: ip=%s ua=%s referer=%s",
        request.headers.get("X-Forwarded-For", request.remote_addr),
        request.headers.get("User-Agent", ""),
        request.headers.get("Referer", ""),
    )
    # Behind TimeWeb's reverse proxy, request.host_url respects X-Forwarded-* headers
    # (see ProxyFix above), so this is always the public https://... origin.
    try:
        form_url = _gpl_init_payment(request.host_url)
    except Exception as exc:
        log.exception("create-payment: GPL init failed: %s", exc)
        return redirect(FAIL_URL, code=302)
    if not form_url:
        log.error("create-payment: no formUrl in GPL response")
        return redirect(FAIL_URL, code=302)
    return redirect(form_url, code=302)


@app.route("/payment-callback", methods=["POST"])
def payment_callback():
    raw = request.get_data(as_text=True)
    log.info("Callback raw body: %s", raw[:2000])

    body = request.get_json(silent=True) or {}
    if not body:
        # Some payment systems send form-encoded — try that too.
        body = request.form.to_dict() if request.form else {}

    log.info("Callback parsed: %s", json.dumps(body, ensure_ascii=False)[:1000])

    raw_haystack = (raw + json.dumps(body, ensure_ascii=False)).lower()
    if not _is_successful(body, raw_haystack):
        log.info("Callback not a success status — ignoring")
        return jsonify({"ok": True, "skipped": "not_success"}), 200

    order_id = _extract_order_id(body, raw)

    with _processed_lock:
        if order_id in _processed:
            log.info("Duplicate callback for order %s — skipping", order_id)
            return jsonify({"ok": True, "skipped": "duplicate"}), 200
        _processed.add(order_id)

    email = _extract_email(body)
    if not email:
        log.error("No email in callback for order %s — body=%s", order_id, body)
        return jsonify({"ok": True, "skipped": "no_email"}), 200

    try:
        invite_link = _tg_create_invite(order_id)
    except Exception as exc:
        log.exception("Telegram createChatInviteLink failed: %s", exc)
        # Откатываем дедуп — иначе при ретрае GPL получит skipped:duplicate.
        with _processed_lock:
            _processed.discard(order_id)
        # 503 → GPL сам повторит колбэк через свой backoff.
        return jsonify({"ok": False, "error": "telegram", "retry": True}), 503

    try:
        _us_send_email(email, invite_link)
    except Exception as exc:
        log.exception("UniSender sendEmail failed: %s", exc)
        with _processed_lock:
            _processed.discard(order_id)
        return jsonify({"ok": False, "error": "unisender", "retry": True}), 503

    log.info("OK: order=%s email=%s", order_id, email)
    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
