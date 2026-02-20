# TG -> VK bridge

Бот пересылает посты из Telegram-канала в стену сообщества VK через VK API, используя `access_token`, автоматически извлеченный из браузерной VK-сессии (Playwright).

Поддерживает:
- текст,
- ссылки `text_link` в формате `текст (https://...)`,
- фото,
- видео,
- media group (несколько вложений в одном посте),
- длинные тексты с разбиением на несколько постов.

## 1. Требования

- Python `3.11+`
- зависимости:
  - `httpx==0.28.1`
  - `python-dotenv==1.0.1`
  - `playwright==1.52.0`
- установленный браузер для Playwright:

```bash
playwright install chromium
```

## 2. Установка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

## 3. Конфиг `.env`

Обязательные:
- `TG_BOT_TOKEN`
- `TG_SOURCE_CHAT_ID`
- `TG_ADMIN_ID`
- `VK_GROUP_ID`
- `VK_STORAGE_STATE_PATH` (файл сессии VK, например `vk_storage_state.json`)

Опциональные:
- `VK_BROWSER_HEADLESS` (`true` по умолчанию)
- `VK_BROWSER_CHANNEL` (например `chrome`, `msedge`; можно пусто)
- `VK_BROWSER_TIMEOUT_SEC` (по умолчанию `60`)
- `VK_MEDIA_TMP_DIR` (временная папка для медиа, по умолчанию `.vk_media_tmp`)
- `STATE_DB_PATH`
- `REPOST_ALL_POSTS`

## 4. Обновление VK-сессии

Локально:

```bash
python vk_session_refresh.py --storage-state vk_storage_state.json
```

Шаги:
1. Откроется окно браузера.
2. Войдите в VK вручную (логин/пароль/2FA), убедитесь, что доступ к вашей группе есть.
3. Нажмите Enter в консоли.
4. Получите файл `vk_storage_state.json`.

Дальше перенесите этот файл на сервер в путь из `VK_STORAGE_STATE_PATH`.

## 5. Запуск

```bash
python app.py
```

Админ-команды в Telegram:
- `/start`, `/status` — состояние бота.
- `/vk_session` или `/check_session` — проверка, что из сохраненной сессии извлекается рабочий web `access_token`.

## 6. Поведение

- Публикация идет через VK API (`wall.post`, `photos.*`, `video.save`), токен берется из браузерной VK-сессии автоматически.
- Если в одном посте более 10 вложений, бот разобьет их на несколько постов.
- Форматирование Telegram (bold/italic/underline/code) не переносится: в VK идет plain text.
- `edited_channel_post` в текущем браузерном режиме не синхронизируется.

## 7. Ограничения

- Сессия VK не вечная: иногда потребуется заново прогнать `vk_session_refresh.py` и обновить файл состояния на сервере.
- Если VK попросит повторный вход/капчу/подтверждение, бот не сможет публиковать, пока сессия не обновлена.
- В текущей реализации вложения поддерживаются только для `image/*` и `video/*`.

## 8. Linux/systemd скрипты

В проекте есть `build.sh`, `start.sh`, `stop.sh`, `update.sh`, `install-service.sh`.

Подготовка:

```bash
chmod +x build.sh start.sh stop.sh update.sh install-service.sh
cp .env.example .env
```

Сборка:

```bash
./build.sh
```

Локальный запуск:

```bash
./start.sh
./stop.sh
```

Установка сервиса:

```bash
./install-service.sh
```

Удаление:

```bash
./install-service.sh --uninstall
```
