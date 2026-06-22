```
                            TG.MaxSyncBot
    _            __     __  _ _             __          __  _
   | |           \ \   / / | | |            \ \        / / | |
   | |__  _   _   \ \_/ /__| | | _____      _\ \  /\  / /__| |__
   | '_ \| | | |   \   / _ \ | |/ _ \ \ /\ / /\ \/  \/ / _ \ '_ \
   | |_) | |_| |    | |  __/ | | (_) \ V  V /  \  /\  /  __/ |_) |
   |_.__/ \__, |    |_|\___|_|_|\___/ \_/\_/    \/  \/ \___|_.__/
           __/ |
          |___/             https://yellowweb.top

If you like this script, PLEASE DONATE!
```

[Support this project](https://yellowweb.top/donate)


# TG -> MAX bridge

Бот пересылает посты из Telegram-канала в канал MAX.

Поддерживает:
- текст и форматирование (bold/italic/underline/code/link/quote),
- медиа (image/video/audio/file),
- media group (несколько вложений одним постом),
- реплаи (TG reply -> MAX reply),
- редактирование постов (`edited_channel_post`),
- длинные тексты с разбиением на несколько сообщений.

Связь с разработчиком: https://t.me/ywbfeedbackbot

## 1. Требования

- Python `3.11+`
- зависимости из `requirements.txt`:
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

## 3. Что заполнить в .env

Обязательные:
- `TG_BOT_TOKEN` — токен Telegram-бота.
- `TG_SOURCE_CHAT_ID` — ID исходного Telegram-канала для мониторинга.
- `TG_ADMIN_ID` — ID админа для команд боту в личке (`/start`, `/status`) и сервисных уведомлений (`OK/ERROR`).
- `MAX_BOT_TOKEN` — токен бота MAX.
- `MAX_TARGET_CHAT_ID` — ID канала/чата в MAX для публикации.

Если обязательные параметры не заданы, бот пишет ошибку в лог и не запускается.

Опциональные:
- `MAX_API_BASE` — по умолчанию `https://platform-api.max.ru`.
- `TG_POLLING_TIMEOUT_SEC` — long polling timeout в секундах (по умолчанию `50`).
- `TG_POLLING_DROP_PENDING_UPDATES` — сбрасывать ли накопленные апдейты при старте polling (`false` по умолчанию).
- `LOG_LEVEL`.
- `STATE_DB_PATH` — путь к SQLite базе маппинга TG<->MAX.
- `REPOST_ALL_POSTS` — режим отбора постов:
  - `true` (по умолчанию): репостятся все посты.
  - `false`: репостятся только посты, где последнее слово начинается с `#`.

## 4. Логика работы

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

## 5. Важные ограничения

- Spoiler в MAX через API не поддерживается: текст под спойлером публикуется как обычный.
- Quote в MAX через API не поддерживается: текст из quote публикуется как обычный текст без доп. оформления.
- Эвристика reply ограничена окном недавних сообщений MAX (API limit по `count` до `100` за запрос).

## 6. Серверные скрипты (Linux/systemd)

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
