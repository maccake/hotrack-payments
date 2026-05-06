"""
Изолированный smoke-test для трёх внешних API: GetPlatinum, Telegram, UniSender.

Запуск:
    python test_integrations.py            # все три теста
    python test_integrations.py gpl        # только GetPlatinum
    python test_integrations.py tg         # только Telegram
    python test_integrations.py us         # только UniSender (отправит реальное письмо)

Перед запуском заполни .env и подгрузи переменные:
    set -a; source .env; set +a; python test_integrations.py
"""
import json
import os
import sys

import requests


def test_gpl():
    """Создаёт тестовый платёж и печатает formUrl. Деньги НЕ списываются — нужно дойти до чек-аута и закрыть."""
    import uuid
    print("\n=== GetPlatinum init-payment-url ===")
    url = f"https://{os.environ['GPL_ACCOUNT']}.getplatinum.ru/api/public/pay/init-payment-url"
    payload = {
        "dealId": f"test-{uuid.uuid4().hex[:12]}",
        "currency": "RUB",
        "amount": int(os.environ.get("PRODUCT_PRICE", 3790)),
        "positions": [
            {
                "prefix": 12,
                "name": os.environ.get("PRODUCT_NAME", "Горячий След"),
                "price": int(os.environ.get("PRODUCT_PRICE", 3790)),
                "quantity": 1,
                "vat": "none",
            }
        ],
        "clientParams": {"clientId": uuid.uuid4().hex},
        "notificationUrl": f"{os.environ['SERVER_BASE_URL'].rstrip('/')}/payment-callback",
        "successUrl": os.environ["SUCCESS_URL"],
        "failUrl": os.environ["FAIL_URL"],
        "customParams": {"test": "true"},
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GPL_API_KEY']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    print(f"HTTP {resp.status_code}")
    print(resp.text[:2000])
    if resp.ok:
        try:
            print("\nParsed:", json.dumps(resp.json(), ensure_ascii=False, indent=2))
        except Exception:
            pass


def test_tg():
    """Создаёт реальный одноразовый инвайт. Если не использовать — он сам истечёт по member_limit."""
    print("\n=== Telegram createChatInviteLink ===")
    url = f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/createChatInviteLink"
    body = {
        "chat_id": int(os.environ["TG_CHANNEL_ID"]),
        "member_limit": 1,
        "name": "smoke-test",
    }
    resp = requests.post(url, json=body, timeout=10)
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))


def test_us():
    """Шлёт реальное письмо на адрес из аргумента или stdin."""
    print("\n=== UniSender sendEmail ===")
    email = sys.argv[2] if len(sys.argv) > 2 else input("Куда слать тест? email: ").strip()
    resp = requests.post(
        "https://api.unisender.com/ru/api/sendEmail",
        params={"format": "json"},
        data={
            "api_key": os.environ["UNISENDER_KEY"],
            "email": email,
            "sender_name": os.environ["UNISENDER_SENDER_NAME"],
            "sender_email": os.environ["UNISENDER_SENDER_EMAIL"],
            "subject": "[TEST] Горячий След — smoke test",
            "body": "<p>Это тестовое письмо. Если ты его видишь — UniSender работает.</p>",
            "list_id": os.environ["UNISENDER_LIST_ID"],
        },
        timeout=15,
    )
    print(f"HTTP {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target in ("gpl", "all"):
        try:
            test_gpl()
        except Exception as e:
            print("GPL test FAILED:", e)
    if target in ("tg", "all"):
        try:
            test_tg()
        except Exception as e:
            print("TG test FAILED:", e)
    if target in ("us", "all"):
        try:
            test_us()
        except Exception as e:
            print("US test FAILED:", e)
