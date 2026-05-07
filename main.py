"""
Payment middleware: Tilda → GetPlatinum → Telegram invite + UniSender email.

Multi-product:
  PRODUCTS dict ниже — единственное место добавления нового продукта.
  Каждый продукт имеет свой URL: /p/<slug>/create-payment
  /create-payment без слага — алиас на дефолтный продукт (hotrack), для совместимости с уже опубликованной кнопкой.

Persistent state в SQLite (/app/data/orders.db):
  - дедуп callback'ов выживает рестарты воркеров/контейнера в пределах деплоя
  - история покупателей (email, phone, invite_link, продукт, время)
  ВАЖНО: TimeWeb Cloud Apps пересоздаёт ФС на каждом git push — БД при деплое сбрасывается.
  Для долгосрочного бэкапа покупатели дополнительно пишутся в лог как BUYER:... (TimeWeb хранит логи).
"""
import hashlib
import html
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, redirect, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# ─────────────────────────── general config ───────────────────────────

GPL_API_KEY = os.environ["GPL_API_KEY"]
GPL_ACCOUNT = os.environ["GPL_ACCOUNT"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
UNISENDER_KEY = os.environ["UNISENDER_KEY"]
UNISENDER_SENDER_NAME = os.environ["UNISENDER_SENDER_NAME"]
UNISENDER_SENDER_EMAIL = os.environ["UNISENDER_SENDER_EMAIL"]

SUPPORT_TG = "https://t.me/gumirovyaroslav"
DB_PATH = Path(os.environ.get("DATA_DIR", "/app/data")) / "orders.db"
CONSENT_TEXT_VERSION = os.environ.get("CONSENT_TEXT_VERSION", "tilda-buy-button-2026-05-07")
CONSENT_TEXT = os.environ.get(
    "CONSENT_TEXT",
    "Нажимая «Забрать», я принимаю оферту, политику обработки персональных данных "
    "и даю согласие на получение рекламно-информационных рассылок.",
)

# Опциональные — для админ-нотификаций и /stats. Если не заданы — функционал просто выключен.
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID")  # numeric id приватного канала/группы куда слать сводку
STATS_TOKEN = os.environ.get("STATS_TOKEN")      # секрет в URL для просмотра статистики

# Telegram API по умолчанию вызываем напрямую. Прокси включается только явным TG_PROXY_ENABLED=1,
# чтобы старый нерабочий TG_PROXY_URL в TimeWeb не задерживал delivery.
TG_PROXY_URL = os.environ.get("TG_PROXY_URL")
TG_PROXY_ENABLED = os.environ.get("TG_PROXY_ENABLED") == "1"
TG_PROXIES = {"https": TG_PROXY_URL, "http": TG_PROXY_URL} if (TG_PROXY_ENABLED and TG_PROXY_URL) else None

# Delivery queue: callback должен отвечать платёжке быстро, а TG/UniSender работают в фоне.
DELIVERY_RETRY_WINDOW_SECONDS = int(os.environ.get("DELIVERY_RETRY_WINDOW_SECONDS", "360"))
DELIVERY_POLL_INTERVAL_SECONDS = float(os.environ.get("DELIVERY_POLL_INTERVAL_SECONDS", "2"))
DELIVERY_PROCESSING_STALE_SECONDS = int(os.environ.get("DELIVERY_PROCESSING_STALE_SECONDS", "120"))

# ─────────────────────────── PRODUCTS REGISTRY ───────────────────────────
# Чтобы добавить продукт: новый ключ в PRODUCTS, обновить кнопку в Tilda на /p/<slug>/create-payment.
# TG-канал: добавь бота админом, дай ему «Создание пригласительных ссылок».
# UniSender: создай отдельный список под покупателей продукта.

PRODUCTS = {
    "hotrack": {
        "name": os.environ.get("PRODUCT_NAME", "Горячий След"),
        "price_rub": int(os.environ.get("PRODUCT_PRICE", "3790")),
        "tg_channel_id": int(os.environ["TG_CHANNEL_ID"]),
        "unisender_list_id": os.environ["UNISENDER_LIST_ID"],
        "success_url": os.environ.get("SUCCESS_URL", "https://gumirovbros.ru/spasibohottrail"),
        "fail_url": os.environ.get("FAIL_URL", "https://gumirovbros.ru/errorhottrail"),
        "email_subject": "Горячий След — ваш доступ готов",
        "email_intro": "Спасибо за покупку. Ниже — личная одноразовая ссылка для входа в закрытый Telegram-канал. Внутри ты сразу увидишь закреплённый PDF-протокол.",
    },
    # ───── шаблон будущего продукта ─────
    # "neurobuilder": {
    #     "name": "NeuroBuilder",
    #     "price_rub": 9990,
    #     "tg_channel_id": int(os.environ["NEUROBUILDER_TG_CHANNEL_ID"]),
    #     "unisender_list_id": os.environ["NEUROBUILDER_UNISENDER_LIST_ID"],
    #     "success_url": "https://gumirovbros.ru/spasibo-neurobuilder",
    #     "fail_url":   "https://gumirovbros.ru/error-neurobuilder",
    #     "email_subject": "NeuroBuilder — ваш доступ готов",
    #     "email_intro":  "...",
    # },
}

DEFAULT_PRODUCT_SLUG = "hotrack"

# ─────────────────────────── app ──────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("payments")

app = Flask(__name__)
# TimeWeb's reverse proxy → доверяем X-Forwarded-* (для request.host_url и rate-limit по реальному IP).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Rate-limit по реальному IP. По умолчанию ничего не лимитирует — только эндпоинты с @limiter.limit.
limiter = Limiter(get_remote_address, app=app, default_limits=[])

# ─────────────────────────── DB ───────────────────────────────

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    with _db() as conn:
        # orders — старт оформления (запись из /create-payment), дедуп и поиск продукта на колбэке.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                deal_id      TEXT PRIMARY KEY,
                product_slug TEXT NOT NULL,
                price_rub    INTEGER NOT NULL,
                ip           TEXT,
                user_agent   TEXT,
                referer      TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        # buyers — успешно обработанные платежи (запись после TG+UniSender). Single source of truth по покупателям.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS buyers (
                deal_id      TEXT PRIMARY KEY,
                product_slug TEXT NOT NULL,
                email        TEXT NOT NULL,
                phone        TEXT,
                invite_link  TEXT,
                amount_rub   REAL,
                completed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # deliveries — очередь выдачи доступа. Позволяет быстро ответить GPL 200, даже если TG лежит.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                deal_id         TEXT PRIMARY KEY,
                product_slug    TEXT NOT NULL,
                email           TEXT NOT NULL,
                phone           TEXT,
                amount_rub      REAL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_error      TEXT,
                invite_link     TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_deliveries_ready
            ON deliveries (status, next_attempt_at, created_at)
        """)


_init_db()

# ─────────────────────────── HTTP helpers ───────────────────────────

_TRANSIENT_EXC = (requests.ConnectionError, requests.Timeout)


def _normalize_admin_chat_id(value: str | None) -> str | None:
    if not value:
        return None
    chat_id = value.strip()
    if not chat_id:
        return None
    if chat_id.startswith("-") or chat_id.startswith("@"):
        return chat_id
    if chat_id.isdigit() and len(chat_id) >= 10:
        return f"-100{chat_id}"
    return chat_id


ADMIN_CHAT_ID = _normalize_admin_chat_id(ADMIN_CHAT_ID_RAW)


def _safe_log_text(value) -> str:
    text = str(value)
    replacements = [TG_BOT_TOKEN, ADMIN_BOT_TOKEN, TG_PROXY_URL]
    for secret in replacements:
        if secret:
            text = text.replace(secret, "***")
    return text


def _post_with_retry(url, *, attempts=3, base_delay=1.0, **kwargs):
    """Ретраим только сетевые сбои. На 4xx/5xx не ретраим — удалённый видел запрос, повтор может задвоить."""
    last_exc = None
    for i in range(attempts):
        try:
            return requests.post(url, **kwargs)
        except _TRANSIENT_EXC as exc:
            last_exc = exc
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                log.warning(
                    "POST %s transient error (%s), retry %d/%d in %.1fs",
                    _safe_log_text(url),
                    _safe_log_text(exc),
                    i + 1,
                    attempts - 1,
                    delay,
                )
                time.sleep(delay)
    raise last_exc


# ─────────────────────────── admin notifications (best-effort) ───────────────────────────

def _tg_notify_admin(text: str) -> None:
    """Отправить сообщение в админ-канал. НИКОГДА не должна валить основной флоу."""
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=5,
            proxies=TG_PROXIES,
        )
        if resp.status_code >= 400:
            log.warning("Admin notify failed: %d %s", resp.status_code, _safe_log_text(resp.text[:500]))
    except Exception as exc:
        log.warning("Admin notify failed (non-critical): %s", _safe_log_text(exc))


# ─────────────────────────── domain ops ───────────────────────────

def _gpl_init_payment(server_base_url: str, product_slug: str, product: dict) -> tuple[str | None, str]:
    """Создаёт платёж в GPL. Возвращает (form_url, deal_id)."""
    deal_id = uuid.uuid4().hex
    client_id = uuid.uuid4().hex
    amount_kopecks = product["price_rub"] * 100  # GPL принимает в копейках
    payload = {
        "dealId": deal_id,
        "currency": "RUB",
        "amount": amount_kopecks,
        "positions": [
            {
                "prefix": 12,
                "name": product["name"],
                "price": amount_kopecks,
                "quantity": 1,
                "vat": "none",
            }
        ],
        "clientParams": {"clientId": client_id},
        "notificationUrl": f"{server_base_url.rstrip('/')}/payment-callback",
        "successUrl": product["success_url"],
        "failUrl": product["fail_url"],
        # product слаг тоже шлём на случай если БД будет пуста (после деплоя) — fallback на колбэке.
        "customParams": {"dealId": deal_id, "product": product_slug},
    }
    url = f"https://{GPL_ACCOUNT}.getplatinum.ru/api/public/pay/init-payment-url"
    headers = {"Authorization": f"Bearer {GPL_API_KEY}", "Content-Type": "application/json"}
    log.info("GPL init-payment-url request: %s", json.dumps(payload, ensure_ascii=False))
    resp = _post_with_retry(url, headers=headers, json=payload, timeout=10)
    log.info("GPL init-payment-url response: %d %s", resp.status_code, resp.text[:1000])
    resp.raise_for_status()
    data = resp.json()
    form_url = (
        data.get("formUrl")
        or data.get("paymentUrl")
        or data.get("url")
        or (data.get("result") or {}).get("formUrl")
        or (data.get("data") or {}).get("formUrl")
    )
    return form_url, deal_id


def _tg_create_invite(channel_id: int, order_id: str, *, attempts: int = 5, timeout: int = 10) -> str:
    """Создаёт одноразовую TG-инвайт-ссылку. RU-IP до api.telegram.org нестабилен — ретрай агрессивный."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/createChatInviteLink"
    body = {"chat_id": channel_id, "member_limit": 1, "name": f"order-{order_id}"[:32]}
    resp = _post_with_retry(url, json=body, timeout=timeout, attempts=attempts, base_delay=1.0, proxies=TG_PROXIES)
    route = "proxy" if TG_PROXIES else "direct"
    log.info("Telegram createChatInviteLink via %s: %d %s", route, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram returned error: {data}")
    return data["result"]["invite_link"]


# ─────────────────────────── delivery queue ───────────────────────────

def _enqueue_delivery(order_id: str, slug: str, email: str, phone: str | None, amount_rub: float | None) -> str:
    """Поставить выдачу доступа в очередь. Возвращает текущий статус delivery."""
    with _db() as conn:
        buyer = conn.execute("SELECT 1 FROM buyers WHERE deal_id=?", (order_id,)).fetchone()
        if buyer:
            return "sent"
        conn.execute(
            """
            INSERT OR IGNORE INTO deliveries
                (deal_id, product_slug, email, phone, amount_rub, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (order_id, slug, email, phone, amount_rub),
        )
        row = conn.execute("SELECT status FROM deliveries WHERE deal_id=?", (order_id,)).fetchone()
    status = row["status"] if row else "unknown"
    log.info("DELIVERY_QUEUED: order=%s product=%s email=%s phone=%s amount_rub=%s status=%s", order_id, slug, email, phone, amount_rub, status)
    return status


def _requeue_stale_deliveries() -> None:
    with _db() as conn:
        conn.execute(
            """
            UPDATE deliveries
            SET status='retrying',
                next_attempt_at=datetime('now'),
                last_error=COALESCE(last_error, 'processing stale after worker restart'),
                updated_at=datetime('now')
            WHERE status='processing'
              AND updated_at <= datetime('now', '-' || ? || ' seconds')
            """,
            (DELIVERY_PROCESSING_STALE_SECONDS,),
        )


def _claim_next_delivery() -> dict | None:
    """Атомарно забрать одну готовую delivery-задачу. BEGIN IMMEDIATE сериализует claim между воркерами."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM deliveries
            WHERE status IN ('pending', 'retrying')
              AND next_attempt_at <= datetime('now')
              AND created_at > datetime('now', '-' || ? || ' seconds')
            ORDER BY created_at
            LIMIT 1
            """,
            (DELIVERY_RETRY_WINDOW_SECONDS,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        attempts = int(row["attempts"]) + 1
        conn.execute(
            """
            UPDATE deliveries
            SET status='processing',
                attempts=?,
                last_error=NULL,
                updated_at=datetime('now')
            WHERE deal_id=?
            """,
            (attempts, row["deal_id"]),
        )
        conn.commit()
        claimed = dict(row)
        claimed["attempts"] = attempts
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_expired_deliveries() -> None:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT deal_id, product_slug, email, phone, amount_rub, last_error
            FROM deliveries
            WHERE status IN ('pending', 'retrying')
              AND created_at <= datetime('now', '-' || ? || ' seconds')
            """,
            (DELIVERY_RETRY_WINDOW_SECONDS,),
        ).fetchall()
        conn.execute(
            """
            UPDATE deliveries
            SET status='failed',
                last_error=COALESCE(last_error, 'retry window expired'),
                updated_at=datetime('now')
            WHERE status IN ('pending', 'retrying')
              AND created_at <= datetime('now', '-' || ? || ' seconds')
            """,
            (DELIVERY_RETRY_WINDOW_SECONDS,),
        )
    for row in rows:
        log.error(
            "DELIVERY_FAILED: order=%s product=%s email=%s phone=%s amount_rub=%s error=%s",
            row["deal_id"], row["product_slug"], row["email"], row["phone"], row["amount_rub"], row["last_error"] or "retry window expired",
        )
        _send_manual_access_email_best_effort(dict(row))


def _finish_delivery(row: dict, invite_link: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO buyers (deal_id, product_slug, email, phone, invite_link, amount_rub) VALUES (?,?,?,?,?,?)",
            (row["deal_id"], row["product_slug"], row["email"], row["phone"], invite_link, row["amount_rub"]),
        )
        conn.execute(
            """
            UPDATE deliveries
            SET status='sent',
                invite_link=?,
                last_error=NULL,
                updated_at=datetime('now')
            WHERE deal_id=?
            """,
            (invite_link, row["deal_id"]),
        )
    log.info(
        "BUYER: order=%s product=%s email=%s phone=%s amount_rub=%s invite=%s",
        row["deal_id"], row["product_slug"], row["email"], row["phone"], row["amount_rub"], invite_link,
    )
    _tg_notify_admin(
        f"✅ <b>Оплата «{html.escape(PRODUCTS[row['product_slug']]['name'])}»</b>\n"
        f"Заказ: <code>{html.escape(row['deal_id'])}</code>\n"
        f"Доступ отправлен"
    )


def _fail_or_retry_delivery(row: dict, exc: Exception) -> None:
    error = _safe_log_text(exc)[:1000]
    attempts = int(row["attempts"])
    created_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - created_at).total_seconds()
    if elapsed >= DELIVERY_RETRY_WINDOW_SECONDS:
        status = "failed"
        next_attempt_at = None
    else:
        status = "retrying"
        delay = min(60, 2 ** min(attempts - 1, 6))
        next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")

    with _db() as conn:
        if next_attempt_at:
            conn.execute(
                """
                UPDATE deliveries
                SET status=?,
                    next_attempt_at=?,
                    last_error=?,
                    updated_at=datetime('now')
                WHERE deal_id=?
                """,
                (status, next_attempt_at, error, row["deal_id"]),
            )
        else:
            conn.execute(
                """
                UPDATE deliveries
                SET status=?,
                    last_error=?,
                    updated_at=datetime('now')
                WHERE deal_id=?
                """,
                (status, error, row["deal_id"]),
            )

    if status == "failed":
        log.error(
            "DELIVERY_FAILED: order=%s product=%s email=%s phone=%s amount_rub=%s attempts=%s error=%s",
            row["deal_id"], row["product_slug"], row["email"], row["phone"], row["amount_rub"], attempts, error,
        )
        _send_manual_access_email_best_effort(row)
        _tg_notify_admin(
            f"🔴 <b>Доступ не отправлен</b>\n"
            f"Заказ: <code>{html.escape(row['deal_id'])}</code>\n"
            f"Проверь логи DELIVERY_FAILED"
        )
    else:
        log.warning("DELIVERY_RETRY: order=%s attempts=%s error=%s", row["deal_id"], attempts, error)


def _process_delivery(row: dict) -> None:
    product = PRODUCTS.get(row["product_slug"])
    if not product:
        raise RuntimeError(f"Unknown product {row['product_slug']}")

    invite_link = row["invite_link"]
    if not invite_link:
        invite_link = _tg_create_invite(product["tg_channel_id"], row["deal_id"], attempts=1, timeout=10)
        with _db() as conn:
            conn.execute(
                "UPDATE deliveries SET invite_link=?, updated_at=datetime('now') WHERE deal_id=?",
                (invite_link, row["deal_id"]),
            )

    _us_send_email(row["email"], invite_link, product)
    _finish_delivery(row, invite_link)


_delivery_worker_started = False
_delivery_worker_lock = threading.Lock()


def _delivery_worker_loop() -> None:
    log.info("Delivery worker loop started pid=%s", os.getpid())
    while True:
        try:
            _requeue_stale_deliveries()
            _mark_expired_deliveries()
            row = _claim_next_delivery()
            if not row:
                time.sleep(DELIVERY_POLL_INTERVAL_SECONDS)
                continue
            log.info("DELIVERY_ATTEMPT: order=%s attempt=%s", row["deal_id"], row["attempts"])
            try:
                _process_delivery(row)
            except Exception as exc:
                log.error("Delivery attempt failed for order %s: %s", row["deal_id"], _safe_log_text(exc))
                _fail_or_retry_delivery(row, exc)
        except Exception as exc:
            log.error("Delivery worker loop error: %s", _safe_log_text(exc))
            time.sleep(DELIVERY_POLL_INTERVAL_SECONDS)


def start_delivery_worker() -> None:
    """Стартует один фоновой delivery-thread внутри текущего gunicorn worker-процесса."""
    global _delivery_worker_started
    with _delivery_worker_lock:
        if _delivery_worker_started:
            return
        thread = threading.Thread(target=_delivery_worker_loop, name="delivery-worker", daemon=True)
        thread.start()
        _delivery_worker_started = True
        log.info("Delivery worker started pid=%s", os.getpid())


def _us_send_email(to_email: str, invite_link: str, product: dict) -> None:
    html = _email_template(invite_link, product)
    resp = _post_with_retry(
        "https://api.unisender.com/ru/api/sendEmail",
        params={"format": "json"},
        data={
            "api_key": UNISENDER_KEY,
            "email": to_email,
            "sender_name": UNISENDER_SENDER_NAME,
            "sender_email": UNISENDER_SENDER_EMAIL,
            "subject": product["email_subject"],
            "body": html,
            "list_id": product["unisender_list_id"],
        },
        timeout=15,
    )
    log.info("UniSender sendEmail: %d %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"UniSender error: {data}")


def _us_send_manual_access_email(to_email: str, product: dict) -> None:
    resp = _post_with_retry(
        "https://api.unisender.com/ru/api/sendEmail",
        params={"format": "json"},
        data={
            "api_key": UNISENDER_KEY,
            "email": to_email,
            "sender_name": UNISENDER_SENDER_NAME,
            "sender_email": UNISENDER_SENDER_EMAIL,
            "subject": f"{product['name']} — оплата прошла, выдадим доступ вручную",
            "body": _manual_access_email_template(product),
            "list_id": product["unisender_list_id"],
        },
        timeout=15,
    )
    log.info("UniSender manual access email: %d %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"UniSender manual access error: {data}")


def _send_manual_access_email_best_effort(row: dict) -> None:
    product = PRODUCTS.get(row["product_slug"])
    if not product:
        log.error("Manual access email skipped for order %s: unknown product %s", row["deal_id"], row["product_slug"])
        return
    try:
        _us_send_manual_access_email(row["email"], product)
        log.info("MANUAL_ACCESS_EMAIL_SENT: order=%s email=%s", row["deal_id"], row["email"])
    except Exception as exc:
        log.error("MANUAL_ACCESS_EMAIL_FAILED: order=%s email=%s error=%s", row["deal_id"], row["email"], _safe_log_text(exc))


def _email_template(invite_link: str, product: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#ece6d8;font-family:'PT Serif',Georgia,serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#ece6d8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="max-width:600px;background:#FFFDF7;padding:32px;border:2px solid #B91C1C;">
        <tr><td>
          <h1 style="margin:0 0 16px 0;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:900;font-size:28px;line-height:1.1;color:#1A1A1A;text-align:center;">
            {product["name"]} — доступ готов
          </h1>
          <p style="margin:0 0 24px 0;font-size:17px;line-height:1.5;color:#1A1A1A;">
            {product["email_intro"]}
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
            Если что-то не сработало — <a href="{SUPPORT_TG}" style="color:#999;">напиши нам в Telegram</a>.<br>
            — Братья Гумировы
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _manual_access_email_template(product: dict) -> str:
    product_name = html.escape(product["name"])
    support_url = html.escape(SUPPORT_TG)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#ece6d8;font-family:'PT Serif',Georgia,serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#ece6d8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="max-width:600px;background:#FFFDF7;padding:32px;border:2px solid #B91C1C;">
        <tr><td>
          <h1 style="margin:0 0 16px 0;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:900;font-size:28px;line-height:1.1;color:#1A1A1A;text-align:center;">
            {product_name} — оплата прошла
          </h1>
          <p style="margin:0 0 18px 0;font-size:17px;line-height:1.5;color:#1A1A1A;">
            Спасибо за покупку. Из-за нестабильной работы Telegram сейчас не получилось автоматически выдать вам доступ в закрытый канал.
          </p>
          <p style="margin:0 0 24px 0;font-size:17px;line-height:1.5;color:#1A1A1A;">
            Напишите нам в Telegram — мы выдадим доступ вручную без проблем.
          </p>
          <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin:24px 0;">
            <tr><td align="center">
              <a href="{support_url}" style="display:inline-block;background:#B91C1C;color:#FFFFFF;text-decoration:none;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:900;font-size:18px;padding:18px 36px;letter-spacing:0.5px;">
                НАПИСАТЬ В TELEGRAM
              </a>
            </td></tr>
          </table>
          <p style="margin:24px 0 0 0;font-size:14px;line-height:1.5;color:#555;text-align:center;">
            — Братья Гумировы
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─────────────────────────── callback parsing ───────────────────────────

def _extract_email(body: dict) -> str | None:
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


def _extract_phone(body: dict) -> str | None:
    return (body.get("clientInfo") or {}).get("phone") or body.get("phone")


def _extract_order_id(body: dict, raw: str) -> str:
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
    if body.get("isSuccess") is True:
        return True
    if body.get("status") in {"success", "paid", "completed", "paymentStatusSuccess"}:
        return True
    if "paymentstatussuccess" in raw_haystack:
        return True
    return False


def _resolve_product(deal_id: str, body: dict) -> tuple[str, dict] | tuple[None, None]:
    """Какой продукт принадлежит этому заказу. Сначала смотрим в БД, потом в customParams (на случай если БД сброшена)."""
    with _db() as conn:
        row = conn.execute("SELECT product_slug FROM orders WHERE deal_id=?", (deal_id,)).fetchone()
    if row:
        slug = row["product_slug"]
    else:
        slug = (body.get("customParams") or {}).get("product")
    if slug and slug in PRODUCTS:
        return slug, PRODUCTS[slug]
    return None, None


# ─────────────────────────── routes ───────────────────────────

@app.route("/p/<slug>/create-payment", methods=["GET"])
# Лимит на воркер: 2 воркера × 3/мин = ~6/мин эффективно с одного IP (in-memory storage не шарится между воркерами).
# Реальному пользователю хватает одного клика; защищает от флуда фейковых заказов в GPL.
@limiter.limit("3 per minute")
def create_payment_for(slug: str):
    if slug not in PRODUCTS:
        abort(404)
    product = PRODUCTS[slug]
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    ua = request.headers.get("User-Agent", "")
    referer = request.headers.get("Referer", "")
    log.info("CONSENT: product=%s ip=%s ua=%s referer=%s", slug, ip, ua, referer)
    try:
        form_url, deal_id = _gpl_init_payment(request.host_url, slug, product)
    except Exception as exc:
        log.exception("create-payment(%s): GPL init failed: %s", slug, exc)
        return redirect(product["fail_url"], code=302)
    if not form_url:
        log.error("create-payment(%s): no formUrl in GPL response", slug)
        return redirect(product["fail_url"], code=302)
    # Защита от open-redirect: form_url ДОЛЖЕН быть https://*.getplatinum.ru.
    # Если GPL когда-то взломан и вернёт чужой URL — мы его не проксируем.
    parsed = urlparse(form_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".getplatinum.ru"):
        log.error("create-payment(%s): suspicious formUrl %s — refusing redirect", slug, form_url)
        return redirect(product["fail_url"], code=302)
    log.info("CONSENT_ORDER: %s", json.dumps({
        "order": deal_id,
        "product": slug,
        "price_rub": product["price_rub"],
        "ip": ip,
        "ua": ua,
        "referer": referer,
        "consent_version": CONSENT_TEXT_VERSION,
        "consent_text": CONSENT_TEXT,
    }, ensure_ascii=False, sort_keys=True))
    # Запишем заказ в БД до редиректа — чтобы на колбэке знать, какой это продукт.
    try:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO orders (deal_id, product_slug, price_rub, ip, user_agent, referer) VALUES (?,?,?,?,?,?)",
                (deal_id, slug, product["price_rub"], ip, ua, referer),
            )
    except Exception as exc:
        log.exception("Failed to record order %s in DB: %s", deal_id, exc)
        # Не валим оплату из-за БД — fallback по customParams.product сработает.
    _tg_notify_admin(
        f"🟡 <b>Клик «{html.escape(product['name'])}»</b>\n"
        f"Сумма: {product['price_rub']} ₽"
    )
    return redirect(form_url, code=302)


@app.route("/create-payment", methods=["GET"])
def create_payment_default():
    """Совместимость со старой кнопкой Tilda. Дефолтный продукт — hotrack."""
    return create_payment_for(DEFAULT_PRODUCT_SLUG)


@app.route("/payment-callback", methods=["POST"])
def payment_callback():
    callback_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    raw = request.get_data(as_text=True)
    log.info("Callback from ip=%s body: %s", callback_ip, raw[:2000])

    body = request.get_json(silent=True) or {}
    if not body:
        body = request.form.to_dict() if request.form else {}
    log.info("Callback parsed: %s", json.dumps(body, ensure_ascii=False)[:1000])

    raw_haystack = (raw + json.dumps(body, ensure_ascii=False)).lower()
    if not _is_successful(body, raw_haystack):
        log.info("Callback not a success status — ignoring")
        return jsonify({"ok": True, "skipped": "not_success"}), 200

    order_id = _extract_order_id(body, raw)

    # Дедуп — атомарно через UNIQUE constraint на buyers.deal_id.
    # Сначала проверим, не обработан ли уже:
    with _db() as conn:
        already = conn.execute("SELECT 1 FROM buyers WHERE deal_id=?", (order_id,)).fetchone()
    if already:
        log.info("Duplicate callback for order %s — skipping", order_id)
        return jsonify({"ok": True, "skipped": "duplicate"}), 200

    slug, product = _resolve_product(order_id, body)
    if not product:
        log.error("Cannot resolve product for order %s — body=%s", order_id, body)
        return jsonify({"ok": True, "skipped": "unknown_product"}), 200

    email = _extract_email(body)
    if not email:
        log.error("No email in callback for order %s — body=%s", order_id, body)
        return jsonify({"ok": True, "skipped": "no_email"}), 200

    phone = _extract_phone(body)
    amount_kopecks = (body.get("paymentData") or {}).get("amount")
    amount_rub = (amount_kopecks / 100) if isinstance(amount_kopecks, (int, float)) else None

    delivery_status = _enqueue_delivery(order_id, slug, email, phone, amount_rub)
    _tg_notify_admin(
        f"✅ <b>Оплата «{html.escape(product['name'])}»</b>\n"
        f"Заказ: <code>{html.escape(order_id)}</code>\n"
        f"Статус доставки: <code>{html.escape(delivery_status)}</code>"
    )
    return jsonify({"ok": True, "delivery": delivery_status}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.errorhandler(429)
def ratelimit_handler(e):
    log.warning("Rate limit hit: %s ip=%s", e, request.headers.get("X-Forwarded-For", request.remote_addr))
    return jsonify({"error": "too_many_requests"}), 429


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_delivery_worker()
    app.run(host="0.0.0.0", port=port, debug=False)
