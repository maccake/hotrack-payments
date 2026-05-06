# Горячий След: как устроен деплой и что проверять

Актуальная рабочая схема на 2026-05-06.

## Коротко

У нас есть две разные части:

1. Сайт на Tilda.
2. Маленький платежный сервер на VPS.

Tilda показывает лендинг, страницы успеха и ошибки. VPS нужен только для оплаты: создать платеж в GetPlatinum, принять callback после успешной оплаты, создать Telegram-инвайт и отправить письмо через UniSender.

## Домены

| Домен | Где живет | Зачем нужен |
|---|---|---|
| `gumirovbros.ru` | Tilda | основной сайт/лендинг |
| `gumirovbros.ru/spasibohottrail` | Tilda | страница после успешной оплаты |
| `gumirovbros.ru/errorhottrail` | Tilda | страница после неуспешной оплаты |
| `brothersgumirov.ru` | Tilda / REG.RU DNS | второй домен |
| `pay.brothersgumirov.ru` | TimeWeb VPS `89.23.99.224` | API платежей |

Важно: HTTPS на основном домене Tilda и HTTPS на `pay.brothersgumirov.ru` - разные сертификаты. Если `brothersgumirov.ru` открывается по HTTPS, это не значит, что `pay.brothersgumirov.ru` тоже уже готов.

## Сервер

VPS:

- провайдер: TimeWeb Cloud;
- IP: `89.23.99.224`;
- имя в панели: `Honest Jackdaw`;
- рабочая папка на сервере: `/opt/payment-server`;
- запуск: Docker Compose;
- публичные порты: `80` и `443`;
- внутренний порт приложения: `8000`.

Пароли, API-ключи и токены нельзя хранить в документации. Они лежат на сервере в `/opt/payment-server/.env` и локально в `payment-server/.env`, если файл есть.

## Файлы в проекте

| Файл | Что делает |
|---|---|
| `payment-server/app.py` | Flask-приложение: создание платежа и callback |
| `payment-server/docker-compose.yml` | запускает `app` и `caddy` |
| `payment-server/Caddyfile` | HTTPS и reverse proxy на Flask |
| `payment-server/Dockerfile` | сборка Python-приложения |
| `payment-server/.env.example` | шаблон переменных окружения |
| `build/tilda/part-A-before-form.html` | первая часть лендинга для Tilda |
| `build/tilda/part-B-form-and-after.html` | форма и нижняя часть лендинга для Tilda |
| `build/tilda/spasibo.html` | HTML-фрагмент страницы спасибо |
| `build/tilda/oshibka.html` | HTML-фрагмент страницы ошибки |

## Поток оплаты

```text
Покупатель на Tilda
  -> нажимает кнопку Купить
  -> GET https://pay.brothersgumirov.ru/create-payment
  -> Flask создает платеж в GetPlatinum
  -> GetPlatinum открывает свою платежную страницу
  -> покупатель платит
  -> GetPlatinum отправляет callback на /payment-callback
  -> Flask создает одноразовую ссылку Telegram
  -> Flask отправляет письмо через UniSender
  -> покупатель видит страницу спасибо на Tilda
```

Если оплата не прошла, покупатель попадает на:

```text
https://gumirovbros.ru/errorhottrail
```

Если оплата прошла:

```text
https://gumirovbros.ru/spasibohottrail
```

## Важные URL в `.env`

На сервере должны быть такие значения:

```env
SUCCESS_URL=https://gumirovbros.ru/spasibohottrail
FAIL_URL=https://gumirovbros.ru/errorhottrail
SERVER_BASE_URL=https://pay.brothersgumirov.ru
```

Если поменять эти значения в `.env`, нужно пересоздать контейнер приложения:

```bash
cd /opt/payment-server
docker compose up -d --force-recreate app
```

## Текущий статус

Что уже было проверено:

- `gumirovbros.ru/spasibohottrail` отдается Tilda с HTTP 200.
- `gumirovbros.ru/errorhottrail` отдается Tilda с HTTP 200.
- `pay.brothersgumirov.ru` указывает на `89.23.99.224`.
- Flask внутри контейнера отвечает на `/health`.
- Безопасный тест callback со статусом `failed` возвращал `{"ok": true, "skipped": "not_success"}` и не отправлял письмо.

Что было проблемой:

- Caddy не мог получить HTTPS-сертификат для `pay.brothersgumirov.ru`.
- Let’s Encrypt видел IP, но получал timeout при проверке.
- ZeroSSL тоже не помог: с VPS зависал исходящий HTTPS-запрос к ZeroSSL.
- После включения whitelist Firewall в TimeWeb был заблокирован SSH, потому что не хватало входящего правила TCP `22`.

## Firewall TimeWeb

Если включен режим `Разрешить трафик`, это whitelist. Все, что не разрешено правилами, будет заблокировано.

Минимум для работы:

Входящий трафик:

| Назначение | Протокол | Порт | Адрес |
|---|---:|---:|---|
| HTTP для проверки сертификата | TCP | `80` | `0.0.0.0/0` |
| HTTPS для API | TCP | `443` | `0.0.0.0/0` |
| SSH-доступ | TCP | `22` | `0.0.0.0/0` |

Исходящий трафик:

Для запуска проще временно разрешить весь исходящий трафик. Если хочется строже, минимум нужен исходящий HTTPS, DNS и доступ к API внешних сервисов.

Практичный вариант на время настройки:

| Назначение | Протокол | Порт | Адрес |
|---|---:|---:|---|
| Исходящий HTTPS | TCP | `443` | `0.0.0.0/0` |
| DNS | UDP | `53` | `0.0.0.0/0` |
| DNS fallback | TCP | `53` | `0.0.0.0/0` |

Если снова пропал SSH-доступ, проверить в TimeWeb именно входящий TCP `22`.

## Команды проверки на сервере

Подключиться:

```bash
ssh root@89.23.99.224
```

Статус контейнеров:

```bash
cd /opt/payment-server
docker compose ps
```

Логи Caddy:

```bash
cd /opt/payment-server
docker compose logs -f caddy
```

Логи приложения:

```bash
cd /opt/payment-server
docker compose logs -f app
```

Проверить health внутри сервера:

```bash
curl -i http://127.0.0.1:8000/health
```

Проверить публичный HTTPS:

```bash
curl -i https://pay.brothersgumirov.ru/health
```

Проверить DNS:

```bash
dig +short pay.brothersgumirov.ru A
```

## Проверки без отправки письма базе

Безопасно:

```bash
curl -i https://pay.brothersgumirov.ru/health
```

Безопасно, если HTTPS уже работает:

```bash
curl -i -X POST https://pay.brothersgumirov.ru/payment-callback \
  -H 'Content-Type: application/json' \
  -d '{"status":"failed","email":"nobody@example.com","orderId":"safe-test"}'
```

Такой callback не должен создавать Telegram-инвайт и не должен отправлять письмо, потому что статус не успешный.

Опасно без понимания:

- отправлять callback со статусом `success`, `paid`, `completed` или `paymentStatusSuccess`;
- запускать `test_integrations.py us` на реальный email базы;
- нажимать реальную оплату много раз.

Успешный callback создает Telegram-инвайт и отправляет письмо.

## Что может сломаться

### 1. `pay.brothersgumirov.ru` не открывается по HTTPS

Проверить:

- A-запись `pay.brothersgumirov.ru -> 89.23.99.224`;
- TimeWeb Firewall: входящие TCP `80`, `443`;
- исходящий трафик с VPS, особенно TCP `443` и DNS;
- логи Caddy.

Главная команда:

```bash
cd /opt/payment-server
docker compose logs --tail=200 caddy
```

### 2. SSH не подключается

Скорее всего TimeWeb Firewall заблокировал входящий TCP `22`.

В панели TimeWeb добавить:

```text
Входящий TCP 22 от 0.0.0.0/0
```

### 3. Кнопка Купить ведет не туда

На Tilda кнопка покупки должна вести на:

```text
https://pay.brothersgumirov.ru/create-payment
```

Но ставить эту ссылку в бой нужно только после того, как HTTPS на `pay.brothersgumirov.ru` работает.

### 4. После оплаты не пришло письмо

Проверить:

- логи `app`;
- пришел ли callback от GetPlatinum;
- есть ли email в теле callback;
- валиден ли `UNISENDER_KEY`;
- верифицирован ли `UNISENDER_SENDER_EMAIL`;
- не попало ли письмо в спам.

### 5. После оплаты нет доступа в Telegram

Проверить:

- бот добавлен админом в Telegram-канал;
- у бота есть право создавать пригласительные ссылки;
- корректный `TG_CHANNEL_ID`;
- не поменяли токен бота.

### 6. Дубли писем/инвайтов

Сейчас защита от дублей хранится в памяти процесса. После перезапуска контейнера она очищается. Для текущего объема это приемлемо, но если GetPlatinum начнет повторять успешные callback после перезапуска, теоретически может уйти повторное письмо.

Если продажи пойдут активнее, заменить in-memory `set()` в `app.py` на SQLite или Redis.

## Как обновлять код на VPS

С локальной машины скопировать измененные файлы на сервер, затем пересобрать:

```bash
scp -r payment-server root@89.23.99.224:/opt/payment-server-new
```

Обычно проще точечно копировать измененный файл:

```bash
scp payment-server/app.py root@89.23.99.224:/opt/payment-server/app.py
scp payment-server/Caddyfile root@89.23.99.224:/opt/payment-server/Caddyfile
```

После изменения Python-кода:

```bash
cd /opt/payment-server
docker compose up -d --build app
```

После изменения Caddyfile:

```bash
cd /opt/payment-server
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
docker compose restart caddy
```

## Rollback

Если после правки стало хуже:

1. вернуть предыдущую версию файла;
2. пересобрать или перезапустить нужный контейнер;
3. смотреть логи.

Команды:

```bash
cd /opt/payment-server
docker compose ps
docker compose logs --tail=100 app
docker compose logs --tail=100 caddy
```

## Что не трогать без причины

- Не удалять `/opt/payment-server/.env`.
- Не публиковать токены и API-ключи в чатах/документах.
- Не менять DNS `gumirovbros.ru`, он обслуживается Tilda.
- Не направлять `gumirovbros.ru` на VPS, иначе можно сломать сайт.
- Не удалять Docker volumes Caddy без причины: там хранятся данные сертификатов.
- Не отправлять успешный тестовый callback на реальные email, если не хотим создавать доступ.

