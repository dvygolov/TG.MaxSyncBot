# TG -> MAX bridge (MVP)

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Webhook endpoint: `POST /tg/webhook`

Healthcheck: `GET /health`

## Flow

1. Receives `channel_post` from Telegram webhook.
2. Checks post text/caption: if the last word starts with `#`, this is an author post.
3. Publishes text and optional media to MAX.
4. Sends admin notification to Telegram about success/failure.
