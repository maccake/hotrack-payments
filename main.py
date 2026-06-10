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
import hmac
import html
import json
import logging
import os
import gzip
import base64
import shutil
import socket
import sqlite3
import tempfile
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
HEALTH_CHECK_INTERVAL_SECONDS = int(os.environ.get("HEALTH_CHECK_INTERVAL_SECONDS", "21600"))
HEALTH_CHECK_POLL_SECONDS = int(os.environ.get("HEALTH_CHECK_POLL_SECONDS", "60"))
HEALTH_ALERT_COOLDOWN_SECONDS = int(os.environ.get("HEALTH_ALERT_COOLDOWN_SECONDS", "900"))
HEALTH_EXTERNAL_URL = os.environ.get("HEALTH_EXTERNAL_URL", "").strip()
S3_BACKUP_ENABLED = os.environ.get("S3_BACKUP_ENABLED") == "1"
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "https://s3.twcstorage.ru").strip()
S3_REGION = os.environ.get("S3_REGION", "ru-1").strip()
S3_BACKUP_PREFIX = os.environ.get("S3_BACKUP_PREFIX", "hottrailpay").strip().strip("/") or "hottrailpay"
S3_BACKUP_MIN_INTERVAL_SECONDS = int(os.environ.get("S3_BACKUP_MIN_INTERVAL_SECONDS", "60"))
S3_BACKUP_STALE_SECONDS = int(os.environ.get("S3_BACKUP_STALE_SECONDS", "900"))
S3_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("S3_CONNECT_TIMEOUT_SECONDS", "5"))
S3_READ_TIMEOUT_SECONDS = int(os.environ.get("S3_READ_TIMEOUT_SECONDS", "10"))
S3_BACKUP_ENCRYPTION_KEY = os.environ.get("S3_BACKUP_ENCRYPTION_KEY", "").strip()
CALLBACK_TOKEN_GRACE_SECONDS = int(os.environ.get("CALLBACK_TOKEN_GRACE_SECONDS", "86400"))
TG_WEBHOOK_FALLBACK_SECRET = hmac.new(
    GPL_API_KEY.encode("utf-8"),
    b"telegram-webhook-v1",
    hashlib.sha256,
).hexdigest()[:48]
TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "").strip()
TG_WEBHOOK_SECRETS = {TG_WEBHOOK_FALLBACK_SECRET}
if TG_WEBHOOK_SECRET:
    TG_WEBHOOK_SECRETS.add(TG_WEBHOOK_SECRET)

REPORT_TZ = timezone(timedelta(hours=3))
REPORT_TZ_LABEL = "МСК"
REPORT_HOUR_LOCAL = 22  # 22:00 по МСК

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
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", "65536"))

# Rate-limit по реальному IP. По умолчанию ничего не лимитирует — только эндпоинты с @limiter.limit.
limiter = Limiter(get_remote_address, app=app, default_limits=[])

# ─────────────────────────── DB ───────────────────────────────

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

S3_BACKUP_LATEST_KEY = f"{S3_BACKUP_PREFIX}/sqlite/latest/orders.db.gz.fernet"
S3_BACKUP_LEGACY_LATEST_KEY = f"{S3_BACKUP_PREFIX}/sqlite/latest/orders.db.gz"
S3_BACKUP_MANIFEST_KEY = f"{S3_BACKUP_PREFIX}/sqlite/manifest.json"
_s3_backup_lock = threading.Lock()
_s3_backup_started = False
_s3_backup_requested = threading.Event()
_s3_backup_last_attempt_at = 0.0
_s3_backup_dirty = False
_s3_restore_blocks_backup = False
_s3_backup_last_status = {
    "enabled": S3_BACKUP_ENABLED,
    "ok": None,
    "status": "disabled" if not S3_BACKUP_ENABLED else "not checked",
    "last_success_at": None,
    "last_error": None,
    "last_key": None,
}


def _mask_secret_text(value) -> str:
    text = str(value)
    replacements = [
        TG_BOT_TOKEN,
        ADMIN_BOT_TOKEN,
        TG_PROXY_URL,
        os.environ.get("AWS_ACCESS_KEY_ID"),
        os.environ.get("AWS_SECRET_ACCESS_KEY"),
    ]
    for secret in replacements:
        if secret:
            text = text.replace(secret, "***")
    return text


def _s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name=S3_REGION,
        config=Config(
            connect_timeout=S3_CONNECT_TIMEOUT_SECONDS,
            read_timeout=S3_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _fernet_key() -> bytes:
    if S3_BACKUP_ENCRYPTION_KEY:
        return S3_BACKUP_ENCRYPTION_KEY.encode("utf-8")
    digest = hashlib.sha256((GPL_API_KEY + ":sqlite-s3-backup:v1").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt_backup_file(src_path: Path, dst_path: Path) -> None:
    from cryptography.fernet import Fernet

    encrypted = Fernet(_fernet_key()).encrypt(src_path.read_bytes())
    dst_path.write_bytes(encrypted)


def _decrypt_backup_file(src_path: Path, dst_path: Path) -> None:
    from cryptography.fernet import Fernet

    decrypted = Fernet(_fernet_key()).decrypt(src_path.read_bytes())
    dst_path.write_bytes(decrypted)


def _download_s3_object_to_file(key: str, dst_path: Path) -> bool:
    try:
        _s3_client().download_file(S3_BUCKET, key, str(dst_path))
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _set_s3_backup_status(
    *,
    ok: bool | None,
    status: str,
    error: str | None = None,
    key: str | None = None,
    success_at: str | None = None,
) -> None:
    with _s3_backup_lock:
        _s3_backup_last_status.update({
            "enabled": S3_BACKUP_ENABLED,
            "ok": ok,
            "status": status,
            "last_error": error,
        })
        if ok:
            _s3_backup_last_status["last_success_at"] = success_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if key:
            _s3_backup_last_status["last_key"] = key


def _refresh_s3_backup_status_from_manifest() -> None:
    if not _s3_configured():
        return
    try:
        obj = _s3_client().get_object(Bucket=S3_BUCKET, Key=S3_BACKUP_MANIFEST_KEY)
        manifest = json.loads(obj["Body"].read().decode("utf-8"))
        created_at = manifest.get("created_at")
        key = manifest.get("snapshot_key") or manifest.get("latest_key")
        if created_at:
            _set_s3_backup_status(ok=True, status="ok", key=key, success_at=created_at)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code in {"404", "NoSuchKey", "NotFound"}:
            return
        _set_s3_backup_status(ok=False, status="manifest check failed", error=_mask_secret_text(exc)[:300])


def _get_s3_backup_status() -> dict:
    _refresh_s3_backup_status_from_manifest()
    with _s3_backup_lock:
        return dict(_s3_backup_last_status)


def _s3_configured() -> bool:
    return S3_BACKUP_ENABLED and bool(S3_BUCKET)


def _restore_db_from_s3_if_needed() -> None:
    global _s3_restore_blocks_backup
    if not _s3_configured() or DB_PATH.exists():
        return
    tmp_dir = Path(tempfile.mkdtemp(prefix="orders-restore-", dir=str(DB_PATH.parent)))
    encrypted_path = tmp_dir / "orders.db.gz.fernet"
    gz_path = tmp_dir / "orders.db.gz"
    restored_path = tmp_dir / "orders.db"
    try:
        log.info("SQLite DB missing, trying S3 restore from s3://%s/%s", S3_BUCKET, S3_BACKUP_LATEST_KEY)
        restored_encrypted = _download_s3_object_to_file(S3_BACKUP_LATEST_KEY, encrypted_path)
        if restored_encrypted:
            _decrypt_backup_file(encrypted_path, gz_path)
            restored_key = S3_BACKUP_LATEST_KEY
            restored_status = "restored encrypted latest backup"
        else:
            restored_legacy = _download_s3_object_to_file(S3_BACKUP_LEGACY_LATEST_KEY, gz_path)
            if not restored_legacy:
                _set_s3_backup_status(ok=None, status="no backup yet")
                log.info("No S3 SQLite backup yet, starting with empty SQLite DB")
                return
            restored_key = S3_BACKUP_LEGACY_LATEST_KEY
            restored_status = "restored legacy plaintext backup"
        with gzip.open(gz_path, "rb") as src, restored_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(restored_path, DB_PATH)
        _set_s3_backup_status(ok=True, status=restored_status, key=restored_key)
        log.info("SQLite DB restored from S3 latest backup")
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        error = _mask_secret_text(exc)[:300]
        _s3_restore_blocks_backup = True
        _set_s3_backup_status(ok=False, status="restore failed; backup blocked", error=error)
        log.error("S3 restore failed, starting with empty SQLite DB and blocking new backups: %s", error)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _copy_sqlite_db(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(str(src_path), timeout=10)
    dst = sqlite3.connect(str(dst_path), timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _perform_s3_backup(reason: str) -> None:
    if not _s3_configured() or not DB_PATH.exists():
        return
    if _s3_restore_blocks_backup:
        log.warning("S3 backup skipped because startup restore failed")
        return
    lock_path = DB_PATH.parent / ".orders-s3-backup.lock"
    lock_file = lock_path.open("a+")
    tmp_dir = Path(tempfile.mkdtemp(prefix="orders-backup-", dir=str(DB_PATH.parent)))
    snapshot_path = tmp_dir / "orders.db"
    gz_path = tmp_dir / "orders.db.gz"
    encrypted_path = tmp_dir / "orders.db.gz.fernet"
    lock_acquired = False
    try:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except BlockingIOError:
            log.info("S3 backup skipped: another worker is backing up")
            return

        _copy_sqlite_db(DB_PATH, snapshot_path)
        with snapshot_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        _encrypt_backup_file(gz_path, encrypted_path)
        digest = hashlib.sha256(encrypted_path.read_bytes()).hexdigest()
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
        snapshot_key = f"{S3_BACKUP_PREFIX}/sqlite/snapshots/orders-{stamp}-{os.getpid()}.db.gz.fernet"
        manifest = {
            "latest_key": S3_BACKUP_LATEST_KEY,
            "snapshot_key": snapshot_key,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "encryption": "fernet",
            "reason": reason,
            "sha256_encrypted": digest,
            "size_bytes": encrypted_path.stat().st_size,
        }
        s3 = _s3_client()
        s3.upload_file(str(encrypted_path), S3_BUCKET, snapshot_key)
        s3.upload_file(str(encrypted_path), S3_BUCKET, S3_BACKUP_LATEST_KEY)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=S3_BACKUP_MANIFEST_KEY,
            Body=json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )
        _set_s3_backup_status(ok=True, status="ok", key=snapshot_key, success_at=manifest["created_at"])
        log.info("S3 backup ok reason=%s key=%s size=%s", reason, snapshot_key, manifest["size_bytes"])
    except Exception as exc:
        error = _mask_secret_text(exc)[:300]
        _set_s3_backup_status(ok=False, status="backup failed", error=error)
        log.error("S3 backup failed reason=%s error=%s", reason, error)
    finally:
        if lock_acquired:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        lock_file.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _s3_backup_loop() -> None:
    global _s3_backup_dirty, _s3_backup_last_attempt_at
    log.info("S3 backup worker started enabled=%s bucket=%s prefix=%s", S3_BACKUP_ENABLED, S3_BUCKET or "-", S3_BACKUP_PREFIX)
    while True:
        _s3_backup_requested.wait(timeout=30)
        _s3_backup_requested.clear()
        now = time.time()
        with _s3_backup_lock:
            dirty = _s3_backup_dirty
            enough_time_passed = now - _s3_backup_last_attempt_at >= S3_BACKUP_MIN_INTERVAL_SECONDS
            if not dirty or not enough_time_passed:
                continue
            _s3_backup_dirty = False
            _s3_backup_last_attempt_at = now
        _perform_s3_backup("background")


def start_s3_backup_worker() -> None:
    global _s3_backup_dirty, _s3_backup_started
    if not _s3_configured():
        return
    with _s3_backup_lock:
        if _s3_backup_started:
            return
        thread = threading.Thread(target=_s3_backup_loop, name="s3-backup", daemon=True)
        thread.start()
        _s3_backup_started = True
        _s3_backup_dirty = True
        _s3_backup_requested.set()


def _request_s3_backup(reason: str) -> None:
    global _s3_backup_dirty
    if not _s3_configured():
        return
    start_s3_backup_worker()
    with _s3_backup_lock:
        _s3_backup_dirty = True
    log.info("S3 backup requested reason=%s", reason)
    _s3_backup_requested.set()


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
                callback_token_required INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN callback_token_required INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS report_log (
                report_key TEXT PRIMARY KEY,
                sent_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_messages (
                message_key TEXT PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                message_id  INTEGER NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_joins (
                deal_id          TEXT PRIMARY KEY,
                product_slug     TEXT,
                email            TEXT,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                telegram_name    TEXT,
                invite_link      TEXT,
                joined_at        TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now'))
            )
        """)

_restore_db_from_s3_if_needed()
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


def _mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "-"
    local, domain = email.split("@", 1)
    if not local:
        return f"***@{domain}"
    if len(local) == 1:
        masked_local = local[0] + "***"
    elif len(local) == 2:
        masked_local = local[0] + "***" + local[-1]
    else:
        masked_local = local[:2] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def _callback_token_for(order_id: str) -> str:
    digest = hmac.new(
        GPL_API_KEY.encode("utf-8"),
        f"gpl-callback-v1:{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:48]


def _callback_token_valid(order_id: str, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(_callback_token_for(order_id), token.strip())


def _legacy_callback_allowed(order_id: str) -> bool:
    """Temporary compatibility for orders created before tokenized callback URLs."""
    try:
        with _db() as conn:
            row = conn.execute(
                """
                SELECT created_at, COALESCE(callback_token_required, 0) AS callback_token_required
                FROM orders
                WHERE deal_id=?
                """,
                (order_id,),
            ).fetchone()
        if not row or int(row["callback_token_required"] or 0):
            return False
        created_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created_at).total_seconds() <= CALLBACK_TOKEN_GRACE_SECONDS
    except Exception as exc:
        log.warning("Legacy callback compatibility check failed for %s: %s", order_id, _safe_log_text(exc))
        return False


def _safe_log_text(value) -> str:
    return _mask_secret_text(value)


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

def _tg_notify_admin(text: str) -> int | None:
    """Отправить сообщение в админ-канал. НИКОГДА не должна валить основной флоу."""
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return None
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
            return None
        data = resp.json()
        return (data.get("result") or {}).get("message_id") if data.get("ok") else None
    except Exception as exc:
        log.warning("Admin notify failed (non-critical): %s", _safe_log_text(exc))
        return None


def _tg_edit_admin_message(message_id: int, text: str) -> bool:
    """Обновить админ-сообщение. Ошибки не должны влиять на оплату или delivery."""
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/editMessageText"
        resp = requests.post(
            url,
            json={
                "chat_id": ADMIN_CHAT_ID,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=5,
            proxies=TG_PROXIES,
        )
        if resp.status_code >= 400:
            if resp.status_code == 400 and "message is not modified" in resp.text.lower():
                return True
            log.warning("Admin edit failed: %d %s", resp.status_code, _safe_log_text(resp.text[:500]))
            return False
        return bool(resp.json().get("ok"))
    except Exception as exc:
        log.warning("Admin edit failed (non-critical): %s", _safe_log_text(exc))
        return False


def _save_admin_message(message_key: str, message_id: int) -> None:
    if not ADMIN_CHAT_ID:
        return
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO admin_messages (message_key, chat_id, message_id, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(message_key) DO UPDATE SET
                chat_id=excluded.chat_id,
                message_id=excluded.message_id,
                updated_at=datetime('now')
            """,
            (message_key, str(ADMIN_CHAT_ID), message_id),
        )


def _get_admin_message_id(message_key: str) -> int | None:
    with _db() as conn:
        row = conn.execute("SELECT message_id FROM admin_messages WHERE message_key=?", (message_key,)).fetchone()
    return int(row["message_id"]) if row else None


def _tg_upsert_admin_message(message_key: str, text: str) -> int | None:
    """Отредактировать известное сообщение или отправить новое и запомнить его."""
    message_id = _get_admin_message_id(message_key)
    if message_id and _tg_edit_admin_message(message_id, text):
        return message_id
    claim_key = f"admin-message:{message_key}"
    if not message_id and not _try_claim_report(claim_key):
        time.sleep(0.2)
        message_id = _get_admin_message_id(message_key)
        if message_id and _tg_edit_admin_message(message_id, text):
            return message_id
        return None
    message_id = _tg_notify_admin(text)
    if message_id:
        _save_admin_message(message_key, message_id)
    else:
        _release_report_claim(claim_key)
    return message_id


def _local_now() -> datetime:
    return datetime.now(REPORT_TZ)


def _utc_to_local_text(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return f"{dt.astimezone(REPORT_TZ).strftime('%d.%m.%Y %H:%M')} {REPORT_TZ_LABEL}"
    except Exception:
        return html.escape(str(value))


def _today_key(prefix: str) -> str:
    return f"{prefix}:{_local_now().strftime('%Y-%m-%d')}"


# ─────────────────────────── daily/weekly/monthly reports ───────────────────────────

def _stats_since(cutoff_utc: str) -> dict:
    with _db() as conn:
        clicks = conn.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (cutoff_utc,)).fetchone()[0]
        payments = conn.execute("SELECT COUNT(*) FROM buyers WHERE completed_at >= ?", (cutoff_utc,)).fetchone()[0]
        revenue = conn.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM buyers WHERE completed_at >= ?", (cutoff_utc,)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM deliveries WHERE status='failed' AND updated_at >= ?", (cutoff_utc,)).fetchone()[0]
    return {
        "clicks": clicks,
        "payments": payments,
        "revenue": revenue or 0,
        "failed": failed,
        "conversion": round(payments / clicks * 100, 1) if clicks > 0 else 0,
    }


def _local_day_start_utc(days_ago: int = 0) -> str:
    now_local = _local_now()
    day = (now_local - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_month_start_utc() -> str:
    now_local = _local_now()
    month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_money(v: float) -> str:
    return f"{v:,.0f} ₽".replace(",", " ")


def _fmt_stats_line(s: dict) -> str:
    line = f"Кликов: {s['clicks']} · Оплат: {s['payments']} · Конверсия: {s['conversion']}%"
    line += f"\nВыручка: {_fmt_money(s['revenue'])}"
    if s["failed"]:
        line += f"\nОшибок доставки: {s['failed']}"
    return line


def _build_daily_report() -> str:
    now_local = _local_now()
    day = _stats_since(_local_day_start_utc(0))
    week = _stats_since(_local_day_start_utc(6))
    month = _stats_since(_local_month_start_utc())

    months_ru = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
                 7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}

    lines = [
        f"<b>Сводка · {now_local.strftime('%d.%m.%Y')} {REPORT_TZ_LABEL}</b>",
        "",
        f"<b>Сегодня</b>",
        _fmt_stats_line(day),
        "",
        f"<b>7 дней</b>",
        _fmt_stats_line(week),
        "",
        f"<b>{months_ru.get(now_local.month, str(now_local.month))}</b>",
        _fmt_stats_line(month),
    ]
    return "\n".join(lines)


def _utc_cutoff_seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _check_db_health() -> tuple[bool, str, dict]:
    with _db() as conn:
        conn.execute("SELECT 1").fetchone()
        queue = dict(conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='retrying' THEN 1 ELSE 0 END) AS retrying,
                SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE
                    WHEN status IN ('pending','retrying','processing')
                     AND created_at <= datetime('now', '-' || ? || ' seconds')
                    THEN 1 ELSE 0 END) AS stale
            FROM deliveries
        """, (DELIVERY_RETRY_WINDOW_SECONDS,)).fetchone())
        last_failed = conn.execute("""
            SELECT deal_id, email, last_error, updated_at
            FROM deliveries
            WHERE status='failed'
            ORDER BY updated_at DESC
            LIMIT 1
        """).fetchone()
        latest_change_at = conn.execute("""
            SELECT MAX(ts) AS latest_change_at
            FROM (
                SELECT created_at AS ts FROM orders
                UNION ALL
                SELECT completed_at AS ts FROM buyers
                UNION ALL
                SELECT updated_at AS ts FROM deliveries
            )
        """).fetchone()["latest_change_at"]
    for key, value in list(queue.items()):
        queue[key] = int(value or 0)
    if last_failed:
        queue["last_failed"] = dict(last_failed)
    queue["latest_change_at"] = latest_change_at
    return True, "ok", queue


def _check_external_health() -> tuple[bool | None, str]:
    if not HEALTH_EXTERNAL_URL:
        return None, "not configured"
    try:
        resp = requests.get(HEALTH_EXTERNAL_URL, timeout=8)
        if resp.status_code == 200:
            return True, "200"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, _safe_log_text(exc)[:200]


def _collect_health_snapshot() -> dict:
    problems = []
    now_local = _local_now()

    try:
        db_ok, db_status, queue = _check_db_health()
    except Exception as exc:
        db_ok, db_status, queue = False, _safe_log_text(exc)[:200], {}
        problems.append(f"DB: {db_status}")

    if not TG_PROXY_ENABLED:
        problems.append("TG proxy выключен")
    if TG_PROXY_ENABLED and (not TG_PROXY_URL or not TG_PROXY_URL.startswith("socks5h://")):
        problems.append("TG proxy URL не socks5h://")

    if queue.get("stale", 0) > 0:
        problems.append(f"зависшие delivery: {queue['stale']}")

    external_ok, external_status = _check_external_health()
    if external_ok is False:
        problems.append(f"external health: {external_status}")

    stats_6h = _stats_since(_utc_cutoff_seconds_ago(6 * 60 * 60)) if db_ok else {
        "clicks": 0, "payments": 0, "revenue": 0, "failed": 0, "conversion": 0
    }
    if stats_6h["failed"]:
        problems.append(f"ошибок доставки за 6ч: {stats_6h['failed']}")

    s3_backup = _get_s3_backup_status()
    if S3_BACKUP_ENABLED and not S3_BUCKET:
        problems.append("S3 backup включён, но S3_BUCKET пустой")
    if S3_BACKUP_ENABLED and s3_backup.get("ok") is False:
        problems.append(f"S3 backup: {s3_backup.get('status')}")
    if S3_BACKUP_ENABLED and s3_backup.get("last_success_at"):
        try:
            last_success = datetime.strptime(s3_backup["last_success_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            latest_change = queue.get("latest_change_at")
            age = (datetime.now(timezone.utc) - last_success).total_seconds()
            if latest_change:
                latest_db_change = datetime.strptime(latest_change, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if latest_db_change > last_success + timedelta(seconds=5) and age > S3_BACKUP_STALE_SECONDS:
                    problems.append(f"S3 backup не обновлялся после изменений в базе {int(age // 60)} мин")
        except Exception:
            pass

    return {
        "ok": not problems,
        "problems": problems,
        "now_local": now_local,
        "db_ok": db_ok,
        "db_status": db_status,
        "queue": queue,
        "stats_6h": stats_6h,
        "external_ok": external_ok,
        "external_status": external_status,
        "s3_backup": s3_backup,
    }


def _format_health_report(snapshot: dict) -> str:
    queue = snapshot["queue"]
    stats_6h = snapshot["stats_6h"]
    s3_backup = snapshot["s3_backup"]
    status_text = "всё спокойно" if snapshot["ok"] else "есть проблемы"
    external_line = (
        f"External /health: {snapshot['external_status']}"
        if snapshot["external_ok"] is not None
        else "External /health: не настроен"
    )
    lines = [
        f"<b>Health check · {snapshot['now_local'].strftime('%d.%m.%Y %H:%M')} {REPORT_TZ_LABEL}</b>",
        f"Статус: {status_text}",
        "",
        f"App: pid={os.getpid()} host={html.escape(socket.gethostname())}",
        f"DB: {'ok' if snapshot['db_ok'] else html.escape(snapshot['db_status'])}",
        f"S3 backup: {html.escape(s3_backup['status'])}" + (f" · {html.escape(s3_backup['last_success_at'])} UTC" if s3_backup.get("last_success_at") else ""),
        f"TG proxy: {'enabled' if TG_PROXY_ENABLED else 'off'} · {'socks5h' if TG_PROXY_URL and TG_PROXY_URL.startswith('socks5h://') else 'check url'}",
        external_line,
        "",
        "<b>Delivery queue</b>",
        (
            f"pending={queue.get('pending', 0)} · retrying={queue.get('retrying', 0)} · "
            f"processing={queue.get('processing', 0)} · failed={queue.get('failed', 0)}"
        ),
        "",
        "<b>За последние 6 часов</b>",
        _fmt_stats_line(stats_6h),
    ]
    if snapshot["problems"]:
        lines.extend(["", "<b>Проблемы</b>"])
        lines.extend(f"• {html.escape(problem)}" for problem in snapshot["problems"])
    return "\n".join(lines)


def _health_alert_key(snapshot: dict) -> str:
    problem_text = "|".join(sorted(snapshot["problems"]))
    digest = hashlib.sha256(problem_text.encode("utf-8")).hexdigest()[:12]
    bucket = int(time.time() // HEALTH_ALERT_COOLDOWN_SECONDS)
    return f"health-alert:{digest}:{bucket}"


_report_scheduler_started = False
_report_scheduler_lock = threading.Lock()
_health_monitor_started = False
_health_monitor_lock = threading.Lock()


def _try_claim_report(key: str) -> bool:
    """Атомарно проверяет и помечает отчёт. BEGIN IMMEDIATE блокирует запись — второй воркер ждёт."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT 1 FROM report_log WHERE report_key=?", (key,)).fetchone()
        if exists:
            conn.rollback()
            return False
        conn.execute("INSERT INTO report_log (report_key) VALUES (?)", (key,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def _release_report_claim(key: str) -> None:
    try:
        with _db() as conn:
            conn.execute("DELETE FROM report_log WHERE report_key=?", (key,))
    except Exception as exc:
        log.warning("Failed to release report claim %s: %s", key, _safe_log_text(exc))


def _report_scheduler_loop() -> None:
    log.info("Report scheduler started pid=%s (daily at %d:00 %s / %d:00 UTC)", os.getpid(), REPORT_HOUR_LOCAL, REPORT_TZ_LABEL, (REPORT_HOUR_LOCAL - 3) % 24)
    while True:
        try:
            now_local = _local_now()
            date_key = now_local.strftime("%Y-%m-%d")
            report_key = f"daily:{date_key}"
            if now_local.hour == REPORT_HOUR_LOCAL and _try_claim_report(report_key):
                report = _build_daily_report()
                _tg_notify_admin(report)
                log.info("Daily report sent for %s", date_key)
        except Exception as exc:
            log.error("Report scheduler error: %s", _safe_log_text(exc))
        time.sleep(30)


def start_report_scheduler() -> None:
    global _report_scheduler_started
    with _report_scheduler_lock:
        if _report_scheduler_started:
            return
        thread = threading.Thread(target=_report_scheduler_loop, name="report-scheduler", daemon=True)
        thread.start()
        _report_scheduler_started = True
        log.info("Report scheduler started pid=%s", os.getpid())


def _health_monitor_loop() -> None:
    log.info(
        "Health monitor started pid=%s interval=%ss alert_cooldown=%ss",
        os.getpid(),
        HEALTH_CHECK_INTERVAL_SECONDS,
        HEALTH_ALERT_COOLDOWN_SECONDS,
    )
    while True:
        try:
            snapshot = _collect_health_snapshot()
            if _try_claim_report(_today_key("health")):
                _tg_upsert_admin_message(_today_key("health-message"), _format_health_report(snapshot))
                log.info("Health check report sent ok=%s", snapshot["ok"])
            else:
                _tg_upsert_admin_message(_today_key("health-message"), _format_health_report(snapshot))
            if not snapshot["ok"] and _try_claim_report(_health_alert_key(snapshot)):
                _tg_notify_admin(_format_health_report(snapshot))
                log.warning("Health alert sent: %s", "; ".join(snapshot["problems"]))
        except Exception as exc:
            log.error("Health monitor error: %s", _safe_log_text(exc))
        time.sleep(HEALTH_CHECK_POLL_SECONDS)


def start_health_monitor() -> None:
    global _health_monitor_started
    with _health_monitor_lock:
        if _health_monitor_started:
            return
        thread = threading.Thread(target=_health_monitor_loop, name="health-monitor", daemon=True)
        thread.start()
        _health_monitor_started = True
        log.info("Health monitor thread started pid=%s", os.getpid())


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
        "notificationUrl": f"{server_base_url.rstrip('/')}/payment-callback/{_callback_token_for(deal_id)}",
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


def _admin_payment_key(deal_id: str) -> str:
    return f"payment:{deal_id}"


def _payment_admin_message(
    *,
    deal_id: str,
    product_slug: str,
    email: str,
    phone: str | None,
    amount_rub: float | None,
    payment_at_utc: str | None,
    access_status: str,
    delivered_at_utc: str | None = None,
    joined_at_utc: str | None = None,
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
) -> str:
    product = PRODUCTS.get(product_slug, {})
    lines = [
        "<b>Оплата получена</b>",
        "",
        f"Продукт: {html.escape(product.get('name', product_slug))}",
        f"Сумма: {_fmt_money(amount_rub or product.get('price_rub', 0))}",
        f"Email: <code>{html.escape(_mask_email(email))}</code>",
    ]
    if phone:
        lines.append(f"Телефон: <code>{html.escape(phone)}</code>")
    lines.extend([
        f"Заказ: <code>{html.escape(deal_id)}</code>",
        f"Оплачено: {_utc_to_local_text(payment_at_utc)}",
        f"Доступ: {html.escape(access_status)}",
    ])
    if delivered_at_utc:
        lines.append(f"Выдано: {_utc_to_local_text(delivered_at_utc)}")
    if joined_at_utc:
        tg_text = str(telegram_user_id) if telegram_user_id else "-"
        if telegram_username:
            tg_text += f" @{telegram_username}"
        lines.extend([
            "Вступление: да",
            f"Вступил: {_utc_to_local_text(joined_at_utc)}",
            f"Telegram: <code>{html.escape(tg_text)}</code>",
        ])
    else:
        lines.append("Вступление: нет")
    return "\n".join(lines)


def _refresh_payment_admin_message(deal_id: str) -> None:
    """Собрать актуальный статус оплаты/выдачи/вступления и обновить одно админ-сообщение."""
    with _db() as conn:
        row = conn.execute(
            """
            SELECT
                d.deal_id,
                d.product_slug,
                d.email,
                d.phone,
                d.amount_rub,
                d.status AS delivery_status,
                d.created_at AS payment_at,
                d.updated_at AS delivery_updated_at,
                b.completed_at AS delivered_at,
                j.telegram_user_id,
                j.telegram_username,
                j.joined_at
            FROM deliveries d
            LEFT JOIN buyers b ON b.deal_id=d.deal_id
            LEFT JOIN telegram_joins j ON j.deal_id=d.deal_id
            WHERE d.deal_id=?
            """,
            (deal_id,),
        ).fetchone()
    if not row:
        return
    status_map = {
        "pending": "в очереди",
        "retrying": "повторная попытка",
        "processing": "выдаем",
        "sent": "выдан",
        "failed": "ошибка выдачи",
    }
    text = _payment_admin_message(
        deal_id=row["deal_id"],
        product_slug=row["product_slug"],
        email=row["email"],
        phone=row["phone"],
        amount_rub=row["amount_rub"],
        payment_at_utc=row["payment_at"],
        access_status=status_map.get(row["delivery_status"], row["delivery_status"] or "-"),
        delivered_at_utc=row["delivered_at"],
        joined_at_utc=row["joined_at"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
    )
    _tg_upsert_admin_message(_admin_payment_key(deal_id), text)


def _refresh_clicks_admin_message(product_slug: str) -> None:
    product = PRODUCTS.get(product_slug, {})
    stats = _stats_since(_local_day_start_utc(0))
    text = "\n".join([
        f"<b>Клики · {html.escape(product.get('name', product_slug))}</b>",
        "",
        f"Дата: {_local_now().strftime('%d.%m.%Y')} {REPORT_TZ_LABEL}",
        _fmt_stats_line(stats),
        f"Обновлено: {_local_now().strftime('%H:%M')} {REPORT_TZ_LABEL}",
    ])
    _tg_upsert_admin_message(_today_key(f"clicks:{product_slug}"), text)


def _record_telegram_join(update: dict) -> bool:
    chat_member = update.get("chat_member") or {}
    new_member = chat_member.get("new_chat_member") or {}
    status = new_member.get("status")
    if status not in {"member", "administrator", "creator"}:
        return False
    user = new_member.get("user") or {}
    if user.get("is_bot"):
        return False
    invite = chat_member.get("invite_link") or {}
    invite_link = invite.get("invite_link")
    invite_name = invite.get("name") or ""
    if not invite_link and not invite_name.startswith("order-"):
        return False

    with _db() as conn:
        row = None
        if invite_link:
            row = conn.execute(
                """
                SELECT deal_id, product_slug, email, invite_link
                FROM deliveries
                WHERE invite_link=?
                UNION ALL
                SELECT deal_id, product_slug, email, invite_link
                FROM buyers
                WHERE invite_link=?
                LIMIT 1
                """,
                (invite_link, invite_link),
            ).fetchone()
        if not row and invite_name.startswith("order-"):
            deal_prefix = invite_name.removeprefix("order-")
            row = conn.execute(
                """
                SELECT deal_id, product_slug, email, invite_link
                FROM deliveries
                WHERE deal_id LIKE ?
                UNION ALL
                SELECT deal_id, product_slug, email, invite_link
                FROM buyers
                WHERE deal_id LIKE ?
                LIMIT 1
                """,
                (f"{deal_prefix}%", f"{deal_prefix}%"),
            ).fetchone()
        if not row:
            return False

        full_name = " ".join(part for part in [user.get("first_name"), user.get("last_name")] if part).strip()
        conn.execute(
            """
            INSERT INTO telegram_joins
                (deal_id, product_slug, email, telegram_user_id, telegram_username, telegram_name, invite_link, joined_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(deal_id) DO UPDATE SET
                telegram_user_id=excluded.telegram_user_id,
                telegram_username=excluded.telegram_username,
                telegram_name=excluded.telegram_name,
                invite_link=excluded.invite_link,
                joined_at=excluded.joined_at,
                updated_at=datetime('now')
            """,
            (
                row["deal_id"],
                row["product_slug"],
                row["email"],
                int(user["id"]),
                user.get("username"),
                full_name or None,
                invite_link or row["invite_link"],
            ),
        )
        deal_id = row["deal_id"]
    log.info(
        "TELEGRAM_JOIN: order=%s email=%s tg_user_id=%s username=%s invite=%s",
        deal_id,
        row["email"],
        user.get("id"),
        user.get("username"),
        invite_link or invite_name,
    )
    _request_s3_backup("telegram join")
    _refresh_payment_admin_message(deal_id)
    return True


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
    _request_s3_backup("delivery queued")
    _refresh_payment_admin_message(order_id)
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
    _request_s3_backup("delivery sent")
    _refresh_payment_admin_message(row["deal_id"])


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
        _request_s3_backup("delivery failed")
        log.error(
            "DELIVERY_FAILED: order=%s product=%s email=%s phone=%s amount_rub=%s attempts=%s error=%s",
            row["deal_id"], row["product_slug"], row["email"], row["phone"], row["amount_rub"], attempts, error,
        )
        _send_manual_access_email_best_effort(row)
        _refresh_payment_admin_message(row["deal_id"])
        _tg_notify_admin(
            f"<b>Доступ не отправлен</b>\n"
            f"Email: <code>{html.escape(_mask_email(row['email']))}</code>\n"
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
        _tg_notify_admin(f"<b>GPL init failed</b>\n{html.escape(product['name'])}\n{html.escape(_safe_log_text(exc)[:200])}")
        return redirect(product["fail_url"], code=302)
    if not form_url:
        log.error("create-payment(%s): no formUrl in GPL response", slug)
        _tg_notify_admin(f"<b>GPL не вернул formUrl</b>\n{html.escape(product['name'])}")
        return redirect(product["fail_url"], code=302)
    parsed = urlparse(form_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".getplatinum.ru"):
        log.error("create-payment(%s): suspicious formUrl %s — refusing redirect", slug, form_url)
        _tg_notify_admin(f"<b>Подозрительный formUrl от GPL</b>\n{html.escape(form_url[:200])}")
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
                """
                INSERT OR REPLACE INTO orders
                    (deal_id, product_slug, price_rub, ip, user_agent, referer, callback_token_required)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (deal_id, slug, product["price_rub"], ip, ua, referer),
            )
        _request_s3_backup("order created")
    except Exception as exc:
        log.exception("Failed to record order %s in DB: %s", deal_id, exc)
        # Не валим оплату из-за БД — fallback по customParams.product сработает.
    _refresh_clicks_admin_message(slug)
    return redirect(form_url, code=302)


@app.route("/create-payment", methods=["GET"])
def create_payment_default():
    """Совместимость со старой кнопкой Tilda. Дефолтный продукт — hotrack."""
    return create_payment_for(DEFAULT_PRODUCT_SLUG)


@app.route("/payment-callback", methods=["POST"])
@app.route("/payment-callback/<callback_token>", methods=["POST"])
def payment_callback(callback_token: str | None = None):
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

    if not _callback_token_valid(order_id, callback_token):
        if not callback_token and _legacy_callback_allowed(order_id):
            log.warning("Accepted legacy unsigned callback for pre-token order %s", order_id)
        else:
            log.warning("Rejected callback with bad token for order %s", order_id)
            _tg_notify_admin(
                f"<b>Отклонён callback без защиты</b>\n"
                f"Заказ: <code>{html.escape(order_id)}</code>\n"
                f"Доступ не выдавался"
            )
            return jsonify({"ok": True, "skipped": "bad_callback_token"}), 200

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
        _tg_notify_admin(f"<b>Неизвестный продукт в callback</b>\nЗаказ: <code>{html.escape(order_id)}</code>")
        return jsonify({"ok": True, "skipped": "unknown_product"}), 200

    email = _extract_email(body)
    if not email:
        log.error("No email in callback for order %s — body=%s", order_id, body)
        _tg_notify_admin(f"<b>Нет email в callback</b>\nЗаказ: <code>{html.escape(order_id)}</code>\nПродукт: {html.escape(slug)}")
        return jsonify({"ok": True, "skipped": "no_email"}), 200

    phone = _extract_phone(body)
    amount_kopecks = (body.get("paymentData") or {}).get("amount")
    amount_rub = (amount_kopecks / 100) if isinstance(amount_kopecks, (int, float)) else None
    if amount_rub is not None and amount_rub + 0.01 < product["price_rub"]:
        log.error("Callback underpaid for order %s: amount_rub=%s expected=%s", order_id, amount_rub, product["price_rub"])
        _tg_notify_admin(
            f"<b>Отклонён callback с неверной суммой</b>\n"
            f"Заказ: <code>{html.escape(order_id)}</code>\n"
            f"Сумма: {html.escape(str(amount_rub))} ₽ вместо {product['price_rub']} ₽"
        )
        return jsonify({"ok": True, "skipped": "amount_mismatch"}), 200

    delivery_status = _enqueue_delivery(order_id, slug, email, phone, amount_rub)
    return jsonify({"ok": True, "delivery": delivery_status}), 200


@app.route("/telegram-webhook/<secret>", methods=["POST"])
def telegram_webhook(secret: str):
    if not any(hmac.compare_digest(secret, allowed) for allowed in TG_WEBHOOK_SECRETS):
        abort(404)
    update = request.get_json(silent=True) or {}
    try:
        handled = _record_telegram_join(update)
        return jsonify({"ok": True, "handled": handled}), 200
    except Exception as exc:
        log.warning("Telegram webhook handling failed: %s", _safe_log_text(exc))
        return jsonify({"ok": True, "handled": False}), 200


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
    start_s3_backup_worker()
    start_report_scheduler()
    start_health_monitor()
    app.run(host="0.0.0.0", port=port, debug=False)
