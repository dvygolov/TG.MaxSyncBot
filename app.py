import asyncio
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from aiohttp import web
from dotenv import load_dotenv


@dataclass
class Settings:
    tg_bot_token: str
    tg_webhook_secret: str | None
    admin_chat_id: int
    max_api_base: str
    max_bot_token: str
    max_target_chat_id: int
    host: str
    port: int


def load_settings() -> Settings:
    load_dotenv()

    def required(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    return Settings(
        tg_bot_token=required("TG_BOT_TOKEN"),
        tg_webhook_secret=os.getenv("TG_WEBHOOK_SECRET") or None,
        admin_chat_id=int(required("ADMIN_CHAT_ID")),
        max_api_base=os.getenv("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/"),
        max_bot_token=required("MAX_BOT_TOKEN"),
        max_target_chat_id=int(required("MAX_TARGET_CHAT_ID")),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8080")),
    )


class BridgeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(30.0, connect=10.0)
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    async def handle_tg_update(self, update: dict[str, Any]) -> None:
        post = update.get("channel_post")
        if not isinstance(post, dict):
            return

        text = (post.get("text") or post.get("caption") or "").strip()
        if not self.is_author_post(text):
            return

        tg_message_id = post.get("message_id")

        try:
            attachments = await self.build_max_attachments(post)
            max_message = await self.send_to_max(text=text, attachments=attachments)
            max_id = self.extract_max_message_id(max_message)
            await self.notify_admin(
                f"OK: post {tg_message_id} published to MAX"
                + (f" (max_id={max_id})" if max_id else "")
            )
        except Exception as exc:
            logging.exception("Failed to publish post %s", tg_message_id)
            await self.notify_admin(f"ERROR: post {tg_message_id} failed: {exc}")

    @staticmethod
    def is_author_post(text: str) -> bool:
        if not text:
            return False
        parts = text.split()
        if not parts:
            return False
        return parts[-1].startswith("#")

    async def build_max_attachments(self, post: dict[str, Any]) -> list[dict[str, Any]]:
        media = self.extract_media(post)
        if media is None:
            return []

        file_id, upload_type, fallback_name, mime_type = media
        file_bytes, file_name = await self.download_tg_file(file_id)
        if not file_name:
            file_name = fallback_name
        if not mime_type:
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        upload_payload = await self.upload_to_max(
            file_bytes=file_bytes,
            file_name=file_name,
            mime_type=mime_type,
            upload_type=upload_type,
        )

        return [{"type": upload_type, "payload": upload_payload}]

    def extract_media(self, post: dict[str, Any]) -> tuple[str, str, str, str] | None:
        photos = post.get("photo")
        if isinstance(photos, list) and photos:
            last_photo = photos[-1]
            file_id = last_photo.get("file_id")
            if file_id:
                return file_id, "image", "photo.jpg", "image/jpeg"

        video = post.get("video")
        if isinstance(video, dict) and video.get("file_id"):
            name = video.get("file_name") or "video.mp4"
            mime_type = video.get("mime_type") or "video/mp4"
            return video["file_id"], "video", name, mime_type

        animation = post.get("animation")
        if isinstance(animation, dict) and animation.get("file_id"):
            name = animation.get("file_name") or "animation.mp4"
            mime_type = animation.get("mime_type") or "video/mp4"
            return animation["file_id"], "video", name, mime_type

        audio = post.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            name = audio.get("file_name") or "audio.mp3"
            mime_type = audio.get("mime_type") or "audio/mpeg"
            return audio["file_id"], "audio", name, mime_type

        document = post.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            name = document.get("file_name") or "document.bin"
            mime_type = document.get("mime_type") or "application/octet-stream"
            return document["file_id"], "file", name, mime_type

        return None

    async def download_tg_file(self, file_id: str) -> tuple[bytes, str | None]:
        get_file_url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/getFile"
        response = await self.http.get(get_file_url, params={"file_id": file_id})
        response.raise_for_status()

        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getFile error: {payload}")

        file_path = payload["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{self.settings.tg_bot_token}/{file_path}"
        file_response = await self.http.get(file_url)
        file_response.raise_for_status()

        return file_response.content, Path(file_path).name

    async def upload_to_max(
        self,
        file_bytes: bytes,
        file_name: str,
        mime_type: str,
        upload_type: str,
    ) -> dict[str, Any]:
        init_url = f"{self.settings.max_api_base}/uploads"
        headers = {"Authorization": self.settings.max_bot_token}

        init_resp = await self.http.post(init_url, headers=headers, params={"type": upload_type})
        init_resp.raise_for_status()
        init_data = init_resp.json()

        upload_url = init_data.get("url")
        if not upload_url:
            raise RuntimeError(f"MAX upload init response has no url: {init_data}")

        files = {"data": (file_name, file_bytes, mime_type)}
        upload_resp = await self.http.post(upload_url, files=files)
        upload_resp.raise_for_status()

        upload_data = upload_resp.json()
        token = upload_data.get("token") or init_data.get("token")
        if token and "token" not in upload_data:
            upload_data["token"] = token
        return upload_data

    async def send_to_max(self, text: str, attachments: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{self.settings.max_api_base}/messages"
        headers = {
            "Authorization": self.settings.max_bot_token,
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "chat_id": self.settings.max_target_chat_id,
            "text": text[:4000],
            "format": "markdown",
        }
        if attachments:
            payload["attachments"] = attachments

        response = await self.http.post(url, headers=headers, json=payload)

        if response.status_code in {429, 500, 502, 503, 504}:
            await asyncio.sleep(1.5)
            response = await self.http.post(url, headers=headers, json=payload)

        response.raise_for_status()
        return response.json()

    async def notify_admin(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendMessage"
        payload = {
            "chat_id": self.settings.admin_chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        try:
            response = await self.http.post(url, json=payload)
            response.raise_for_status()
        except Exception:
            logging.exception("Failed to notify admin")

    @staticmethod
    def extract_max_message_id(response_json: dict[str, Any]) -> str | None:
        message = response_json.get("message")
        if isinstance(message, dict):
            return message.get("message_id") or message.get("id")
        return response_json.get("message_id") or response_json.get("id")


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def tg_webhook(request: web.Request) -> web.Response:
    service: BridgeService = request.app["bridge_service"]
    settings: Settings = request.app["settings"]

    if settings.tg_webhook_secret:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != settings.tg_webhook_secret:
            return web.Response(status=403, text="invalid secret")

    try:
        update = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    asyncio.create_task(service.handle_tg_update(update))
    return web.json_response({"ok": True})


async def on_cleanup(app: web.Application) -> None:
    service: BridgeService = app["bridge_service"]
    await service.close()


def create_app() -> web.Application:
    settings = load_settings()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    app = web.Application()
    app["settings"] = settings
    app["bridge_service"] = BridgeService(settings)

    app.router.add_get("/health", health)
    app.router.add_post("/tg/webhook", tg_webhook)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    application = create_app()
    conf: Settings = application["settings"]
    web.run_app(application, host=conf.host, port=conf.port)
