# Горячий След — payment middleware

Flask-сервис между Tilda, GetPlatinum, Telegram и UniSender.
Развёртывается на **TimeWeb Cloud Apps** с автоматическим HTTPS.

## Поток

```
Tilda кнопка
  → GET /create-payment
  → GetPlatinum init payment → покупатель платит
  → POST /payment-callback
      ├─ Telegram createChatInviteLink (member_limit=1)
      └─ UniSender sendEmail с инвайт-ссылкой
```

## Файлы

| Файл | Зачем |
|---|---|
| `main.py` | Flask-приложение |
| `gunicorn.conf.py` | конфиг gunicorn для TimeWeb Cloud Apps |
| `requirements.txt` | зависимости |
| `.env.example` | список переменных окружения |
| `test_integrations.py` | smoke-тесты GPL/TG/UniSender |
| `_archive-vps/` | старые Caddy/Docker-файлы для VPS-варианта (на случай отката) |

## Локальный запуск

```bash
cp .env.example .env
# заполнить .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
python main.py            # http://localhost:8080
```

## Деплой на TimeWeb Cloud Apps

1. В панели TimeWeb → **Cloud Apps → Создать** → подключаешь этот git-репозиторий.
2. Тип приложения: **Backend → Python (Flask)**.
3. В разделе **Переменные** копируешь содержимое `.env` (ключ-значение).
4. Деплой стартует автоматически. После сборки получаешь URL вида `https://имя-приложения.twc1.net` с валидным HTTPS.

После деплоя:
- В `SERVER_BASE_URL` (env var) подставить выданный URL `https://имя.twc1.net`.
- На лендинге Tilda кнопку «Купить» направить на `https://имя.twc1.net/create-payment`.
- Smoke-тест: `curl https://имя.twc1.net/health` → `{"status":"ok"}`.

## Smoke-тесты

```bash
set -a; source .env; set +a
python test_integrations.py tg                 # Telegram: бот админ + member_limit
python test_integrations.py us your@email.com  # UniSender: тестовое письмо
python test_integrations.py gpl                # GetPlatinum: тестовый платёж
```

## Защита от дублей

In-memory `set()`. Перезапуск приложения = забыли все обработанные платежи.
Для текущего объёма (десятки покупок в день) приемлемо: GetPlatinum не повторяет
callback повторно после первого 200 OK. Если объём вырастет — заменить на SQLite/Redis.
