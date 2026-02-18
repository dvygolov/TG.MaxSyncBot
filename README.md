# TG -> MAX bridge

Бот пересылает посты из Telegram-канала в канал MAX.

Поддерживает:
- текст и форматирование (bold/italic/underline/code/link/quote),
- медиа (image/video/audio/file),
- media group (несколько вложений одним постом),
- реплаи (TG reply -> MAX reply),
- редактирование постов (`edited_channel_post`),
- длинные тексты с разбиением на несколько сообщений.

## 1. Требования

- Python `3.11+`
- зависимости из `requirements.txt`:
  - `aiohttp==3.11.13`
  - `httpx==0.28.1`
  - `python-dotenv==1.0.1`

## 2. Установка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Запуск:

```bash
python app.py
```

Эндпоинты:
- `GET /health` (только в режиме `webhook`)
- `POST /tg/webhook` (только в режиме `webhook`)

## 3. Что заполнить в .env

Обязательные:
- `TG_BOT_TOKEN` — токен Telegram-бота.
- `MAX_BOT_TOKEN` — токен бота MAX.
- `MAX_TARGET_CHAT_ID` — ID канала/чата в MAX для публикации.

Опциональные:
- `TG_SOURCE_CHAT_ID` — ID исходного Telegram-канала для мониторинга. Если задан, бот обрабатывает только этот канал.
- `TG_ADMIN_ID` — ID админа для команд боту в личке (`/start`, `/status`) и сервисных уведомлений (`OK/ERROR`).
- `MAX_API_BASE` — по умолчанию `https://platform-api.max.ru`.
- `TG_UPDATE_MODE` — режим получения апдейтов из Telegram:
  - `webhook` (по умолчанию),
  - `polling` (без webhook и без публичного URL).
- `TG_WEBHOOK_SECRET` — секрет Telegram webhook (используется в режиме `webhook`).
- `TG_POLLING_TIMEOUT_SEC` — long polling timeout в секундах (по умолчанию `50`).
- `TG_POLLING_DROP_PENDING_UPDATES` — сбрасывать ли накопленные апдейты при старте polling (`false` по умолчанию).
- `APP_HOST`, `APP_PORT`, `LOG_LEVEL`.
- `STATE_DB_PATH` — путь к SQLite базе маппинга TG<->MAX.
- `REPOST_ALL_POSTS` — режим отбора постов:
  - `true` (по умолчанию): репостятся все посты.
  - `false`: репостятся только посты, где последнее слово начинается с `#`.

## 4. Как получить ID и токены

### Telegram

1. Токен бота:
- создать бота через `@BotFather`,
- взять `TG_BOT_TOKEN`.

2. `TG_SOURCE_CHAT_ID` (опционально, но рекомендуется):
- ID канала, который нужно мониторить.
- можно указать полный ID канала `-100...` или короткий `123...`.

3. `TG_ADMIN_ID` (опционально, но рекомендуется):
- числовой ID пользователя-админа.
- если задан: может писать боту `/start` и `/status`, и получает сервисные `OK/ERROR` уведомления.

4. Дать боту права в исходном канале:
- бот должен быть админом, если нужен стабильный доступ к постам/медиа.

### MAX

1. Создать/получить токен бота MAX -> `MAX_BOT_TOKEN`.
2. Добавить бота в целевой канал MAX с правом публикации.
3. Взять `MAX_TARGET_CHAT_ID` (числовой ID канала).

## 5. Режимы Telegram: webhook и polling

### Webhook режим (`TG_UPDATE_MODE=webhook`)

Используется входящий HTTP endpoint `POST /tg/webhook`.

Пример установки webhook:

```bash
https://api.telegram.org/bot<TG_BOT_TOKEN>/setWebhook?url=https://your-domain/tg/webhook
```

Если используете секрет:
- задайте `TG_WEBHOOK_SECRET` в `.env`,
- при `setWebhook` передайте тот же secret.

### Polling режим (`TG_UPDATE_MODE=polling`)

- Публичный домен/HTTPS не нужен.
- Бот сам вызывает `getUpdates` и обрабатывает `channel_post`/`edited_channel_post`.
- При старте бот автоматически отключает webhook (`setWebhook` с пустым `url`).

## 6. Логика работы

### Публикация

- `REPOST_ALL_POSTS=true`: любой пост репостится в MAX.
- `REPOST_ALL_POSTS=false`: репост только если последнее слово — хэштег.

### Длинные тексты

- лимит MAX: `4000` символов в `request.text`.
- если текст длиннее, бот режет его по абзацу/переносу/слову около `2000`.
- в конец промежуточной части добавляет:
  - `👇 ПРОДОЛЖЕНИЕ В СЛЕДУЮЩЕМ ПОСТЕ`
- следующая часть отправляется reply на предыдущую.

### Media group

- несколько вложений из одного TG `media_group` собираются в один пост в MAX.

### Реплаи

- если TG пост является reply, бот пытается найти соответствующий пост в MAX:
  - сначала по локальному маппингу (SQLite),
  - если маппинга нет — по эвристике сравнения текста среди последних сообщений MAX.

### Редактирование постов

При `edited_channel_post`:
1. если пост уже есть в MAX -> бот редактирует текст в MAX,
2. если поста в MAX нет, но после редактирования стал подходящим по фильтру -> бот публикует его.

## 7. Важные ограничения

- Spoiler в MAX через API не поддерживается: текст под спойлером публикуется как обычный.
- Для видео в MAX иногда нужно время на обработку (`attachment.not.ready`): бот уже делает ретраи.
- Эвристика reply ограничена окном недавних сообщений MAX (API limit по `count` до `100` за запрос).
- В режиме `polling` запускайте только один инстанс бота на один токен Telegram.

## 8. Рекомендации

- Не коммитьте `.env` в репозиторий.
- Храните `TG_BOT_TOKEN` и `MAX_BOT_TOKEN` как секреты.
- Проверяйте webhook командой `getWebhookInfo` в Telegram API.

## 9. Серверные скрипты (Linux/systemd)

В проекте есть `build.sh`, `start.sh`, `stop.sh`, `update.sh`, `install-service.sh`.
Они рассчитаны на запуск на Linux-сервере (Ubuntu/Debian/CentOS) и повторяют типовой флоу "собрать -> поставить сервис -> обновлять одной командой".

Подготовка:

```bash
chmod +x build.sh start.sh stop.sh update.sh install-service.sh
cp .env.example .env
# заполните .env
```

Сборка окружения:

```bash
./build.sh
```

Локальный запуск (без systemd):

```bash
./start.sh
./stop.sh
```

Установка systemd-сервиса:

```bash
./install-service.sh
```

Скрипт сохраняет имя установленного сервиса в `.service-name`, и `./update.sh` затем использует его автоматически.

По умолчанию имя сервиса: `tg-maxsyncbot`.
Можно задать своё:

```bash
./install-service.sh my-custom-service
```

Для неинтерактивной установки можно явно задать Linux-пользователя сервиса:

```bash
SERVICE_USER=deploy ./install-service.sh
```

Удаление сервиса:

```bash
./install-service.sh --uninstall
```

Обновление на сервере (git pull + rebuild + restart сервиса):

```bash
./update.sh
```

Если сервис называется не `tg-maxsyncbot`, передайте имя через переменную:

```bash
SERVICE_NAME=my-custom-service ./update.sh
```
