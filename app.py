from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from dotenv import load_dotenv


@dataclass
class Settings:
    tg_bot_token: str
    tg_source_chat_id: int
    tg_admin_id: int
    tg_polling_timeout_sec: int
    tg_polling_drop_pending_updates: bool
    max_api_base: str
    max_bot_token: str
    max_target_chat_id: int
    state_db_path: str
    repost_all_posts: bool


def load_settings() -> Settings:
    load_dotenv()

    def required(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    def env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def env_int(name: str, default: int, min_value: int | None = None) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = int(raw.strip())
        except Exception:
            return default
        if min_value is not None and value < min_value:
            return min_value
        return value

    def required_int(name: str) -> int:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            raise RuntimeError(f"Missing required environment variable: {name}")
        try:
            return int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer, got: {raw}") from exc

    return Settings(
        tg_bot_token=required("TG_BOT_TOKEN"),
        tg_source_chat_id=required_int("TG_SOURCE_CHAT_ID"),
        tg_admin_id=required_int("TG_ADMIN_ID"),
        tg_polling_timeout_sec=env_int("TG_POLLING_TIMEOUT_SEC", 50, min_value=1),
        tg_polling_drop_pending_updates=env_bool("TG_POLLING_DROP_PENDING_UPDATES", False),
        max_api_base=os.getenv("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/"),
        max_bot_token=required("MAX_BOT_TOKEN"),
        max_target_chat_id=required_int("MAX_TARGET_CHAT_ID"),
        state_db_path=os.getenv("STATE_DB_PATH", "bridge_state.db"),
        repost_all_posts=env_bool("REPOST_ALL_POSTS", True),
    )


class MessageMapStore:
    SEPARATOR = "||"

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = Lock()
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_max_message_map (
                tg_chat_id INTEGER NOT NULL,
                tg_message_id INTEGER NOT NULL,
                max_message_id TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (tg_chat_id, tg_message_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bridge_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )
        self.conn.commit()

    @classmethod
    def serialize_ids(cls, max_message_ids: list[str]) -> str:
        cleaned = [message_id for message_id in max_message_ids if message_id]
        return cls.SEPARATOR.join(cleaned)

    @classmethod
    def deserialize_ids(cls, raw_value: str | None) -> list[str]:
        if not raw_value:
            return []
        return [part for part in raw_value.split(cls.SEPARATOR) if part]

    def put_ids(self, tg_chat_id: int, tg_message_id: int, max_message_ids: list[str]) -> None:
        raw_value = self.serialize_ids(max_message_ids)
        if not raw_value:
            return
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO tg_max_message_map (tg_chat_id, tg_message_id, max_message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(tg_chat_id, tg_message_id) DO UPDATE SET
                    max_message_id=excluded.max_message_id,
                    created_at=strftime('%s','now')
                """,
                (tg_chat_id, tg_message_id, raw_value),
            )
            self.conn.commit()

    def put(self, tg_chat_id: int, tg_message_id: int, max_message_id: str) -> None:
        self.put_ids(tg_chat_id, tg_message_id, [max_message_id])

    def get_ids(self, tg_chat_id: int, tg_message_id: int) -> list[str]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT max_message_id
                FROM tg_max_message_map
                WHERE tg_chat_id = ? AND tg_message_id = ?
                """,
                (tg_chat_id, tg_message_id),
            ).fetchone()
        if not row:
            return []
        return self.deserialize_ids(row[0])

    def get(self, tg_chat_id: int, tg_message_id: int) -> str | None:
        ids = self.get_ids(tg_chat_id, tg_message_id)
        return ids[0] if ids else None

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def set_state(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO bridge_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=strftime('%s','now')
                """,
                (key, value),
            )
            self.conn.commit()

    def get_state(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT value
                FROM bridge_state
                WHERE key = ?
                """,
                (key,),
            ).fetchone()
        if not row:
            return None
        return row[0]


class BridgeService:
    MAX_TEXT_LIMIT = 4000
    SPLIT_TARGET = 2000
    SPLIT_MIN = 600
    CONTINUATION_SUFFIX = "\n\n👇 ПРОДОЛЖЕНИЕ В СЛЕДУЮЩЕМ ПОСТЕ"
    MEDIA_GROUP_DELAY_SECONDS = 1.8
    TG_POLL_OFFSET_STATE_KEY = "tg_polling_offset"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(30.0, connect=10.0)
        self.http = httpx.AsyncClient(timeout=timeout)
        self.message_map = MessageMapStore(settings.state_db_path)
        self.media_group_posts: dict[str, list[dict[str, Any]]] = {}
        self.media_group_tasks: dict[str, asyncio.Task[None]] = {}
        self.media_group_archive: dict[str, list[dict[str, Any]]] = {}
        self.media_group_lock = asyncio.Lock()

    async def close(self) -> None:
        tasks = list(self.media_group_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.media_group_tasks.clear()
        self.media_group_posts.clear()
        self.media_group_archive.clear()
        await self.http.aclose()
        self.message_map.close()

    async def handle_tg_update(self, update: dict[str, Any]) -> None:
        post = update.get("channel_post")
        if isinstance(post, dict):
            post_chat_id = self.extract_tg_chat_id(post)
            if self.is_allowed_source_chat(post_chat_id):
                media_group_id = post.get("media_group_id")
                if isinstance(media_group_id, str) and media_group_id:
                    await self.enqueue_media_group_post(post)
                else:
                    await self.process_single_post(post)

        edited_post = update.get("edited_channel_post")
        if isinstance(edited_post, dict):
            edited_chat_id = self.extract_tg_chat_id(edited_post)
            if self.is_allowed_source_chat(edited_chat_id):
                await self.process_edited_post(edited_post)

        message = update.get("message")
        if isinstance(message, dict):
            await self.process_admin_command(message)

    async def process_admin_command(self, message: dict[str, Any]) -> None:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        command = text.strip().split()[0].split("@")[0].lower()
        if command not in {"/start", "/status"}:
            return

        if not self.is_admin_message(message):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        response_text = (
            "Привет, админ. Я работаю.\n"
            f"Источник TG: {self.settings.tg_source_chat_id}\n"
            f"Назначение MAX: {self.settings.max_target_chat_id}"
        )
        await self.send_tg_message(chat_id=chat_id, text=response_text)

    def is_admin_message(self, message: dict[str, Any]) -> bool:
        admin_id = self.settings.tg_admin_id

        from_user = message.get("from")
        from_id = from_user.get("id") if isinstance(from_user, dict) else None
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None

        return from_id == admin_id or chat_id == admin_id

    def is_allowed_source_chat(self, tg_chat_id: int | None) -> bool:
        source_chat_id = self.settings.tg_source_chat_id
        if tg_chat_id is None:
            return False

        candidates: set[int] = {source_chat_id}
        if source_chat_id > 0:
            # Allow short channel id format (e.g. 12345 -> -10012345).
            candidates.add(int(f"-100{source_chat_id}"))
        elif str(source_chat_id).startswith("-100"):
            # Allow both full channel id and short id representations.
            candidates.add(int(str(source_chat_id)[4:]))

        return tg_chat_id in candidates

    async def enqueue_media_group_post(self, post: dict[str, Any]) -> None:
        key = self.media_group_key(post)
        async with self.media_group_lock:
            self.media_group_posts.setdefault(key, []).append(post)
            task = self.media_group_tasks.get(key)
            if task is None or task.done():
                self.media_group_tasks[key] = asyncio.create_task(self.flush_media_group(key))

    async def flush_media_group(self, key: str) -> None:
        await asyncio.sleep(self.MEDIA_GROUP_DELAY_SECONDS)
        async with self.media_group_lock:
            posts = self.media_group_posts.pop(key, [])
            self.media_group_tasks.pop(key, None)

        if not posts:
            return

        posts.sort(key=lambda p: int(p.get("message_id") or 0))
        await self.process_media_group(posts)

    async def process_single_post(self, post: dict[str, Any]) -> None:
        source_text, entities = self.extract_text_and_entities(post)
        if not self.should_repost(source_text):
            return

        tg_message_id = self.extract_tg_message_id(post)
        tg_chat_id = self.extract_tg_chat_id(post)

        try:
            text = self.convert_tg_entities_to_html(source_text, entities).strip()
            attachments = await self.build_max_attachments(post)
            if not text and not attachments:
                return
            max_ids = await self.publish_to_max(
                text=text,
                attachments=attachments,
                post=post,
            )
            max_id = max_ids[0] if max_ids else None
            if max_id and tg_chat_id is not None and tg_message_id is not None:
                self.message_map.put_ids(tg_chat_id, tg_message_id, max_ids)
            await self.notify_admin(
                f"OK: post {tg_message_id} published to MAX"
                + (f" (max_id={max_id})" if max_id else "")
            )
        except Exception as exc:
            logging.exception("Failed to publish post %s", tg_message_id)
            await self.notify_admin(f"ERROR: post {tg_message_id} failed: {exc}")

    async def process_media_group(self, posts: list[dict[str, Any]]) -> None:
        lead_post = posts[0]
        source_text = ""
        source_entities: list[dict[str, Any]] = []
        for post in posts:
            text, entities = self.extract_text_and_entities(post)
            if text.strip():
                source_text = text
                source_entities = entities
                break

        group_key = self.media_group_key(lead_post)
        if not self.should_repost(source_text):
            self.media_group_archive[group_key] = posts
            return

        tg_chat_id = self.extract_tg_chat_id(lead_post)
        lead_message_id = self.extract_tg_message_id(lead_post)
        self.media_group_archive.pop(group_key, None)

        try:
            text = self.convert_tg_entities_to_html(source_text, source_entities).strip()
            attachments: list[dict[str, Any]] = []
            for post in posts:
                attachments.extend(await self.build_max_attachments(post))

            max_ids = await self.publish_to_max(
                text=text,
                attachments=attachments,
                post=lead_post,
            )
            max_id = max_ids[0] if max_ids else None
            if max_id and tg_chat_id is not None:
                for post in posts:
                    tg_message_id = self.extract_tg_message_id(post)
                    if tg_message_id is not None:
                        self.message_map.put_ids(tg_chat_id, tg_message_id, max_ids)
            await self.notify_admin(
                f"OK: media group lead {lead_message_id} ({len(posts)} items) published to MAX"
                + (f" (max_id={max_id})" if max_id else "")
            )
        except Exception as exc:
            logging.exception("Failed to publish media group lead %s", lead_message_id)
            await self.notify_admin(
                f"ERROR: media group lead {lead_message_id} ({len(posts)} items) failed: {exc}"
            )

    async def process_edited_post(self, post: dict[str, Any]) -> None:
        tg_message_id = self.extract_tg_message_id(post)
        tg_chat_id = self.extract_tg_chat_id(post)
        source_text, entities = self.extract_text_and_entities(post)
        should_repost = self.should_repost(source_text)

        existing_max_ids = await self.resolve_existing_max_ids(post)
        if existing_max_ids:
            try:
                text = self.convert_tg_entities_to_html(source_text, entities).strip()
                updated_ids = await self.update_existing_max_post(
                    existing_max_ids=existing_max_ids,
                    text=text,
                )
                if tg_chat_id is not None and tg_message_id is not None and updated_ids:
                    self.message_map.put_ids(tg_chat_id, tg_message_id, updated_ids)
                await self.notify_admin(
                    f"OK: edited post {tg_message_id} synced to MAX"
                    + (f" (max_id={updated_ids[0]})" if updated_ids else "")
                )
            except Exception as exc:
                logging.exception("Failed to sync edited post %s", tg_message_id)
                await self.notify_admin(
                    f"ERROR: edited post {tg_message_id} sync failed: {exc}"
                )
            return

        if should_repost:
            media_group_id = post.get("media_group_id")
            if isinstance(media_group_id, str) and media_group_id:
                key = self.media_group_key(post)
                archived = self.media_group_archive.get(key)
                if archived:
                    edited_message_id = self.extract_tg_message_id(post)
                    merged: list[dict[str, Any]] = []
                    replaced = False
                    for item in archived:
                        if self.extract_tg_message_id(item) == edited_message_id:
                            merged.append(post)
                            replaced = True
                        else:
                            merged.append(item)
                    if not replaced:
                        merged.append(post)
                    self.media_group_archive.pop(key, None)
                    await self.process_media_group(merged)
                    return
            await self.process_single_post(post)

    def should_repost(self, text: str) -> bool:
        if self.settings.repost_all_posts:
            return True
        return self.is_author_post((text or "").strip())

    @staticmethod
    def is_author_post(text: str) -> bool:
        if not text:
            return False
        parts = text.split()
        if not parts:
            return False
        return parts[-1].startswith("#")

    @staticmethod
    def extract_text_and_entities(post: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        text = post.get("text")
        if isinstance(text, str):
            entities = post.get("entities")
            if isinstance(entities, list):
                return text, [e for e in entities if isinstance(e, dict)]
            return text, []

        caption = post.get("caption")
        if isinstance(caption, str):
            entities = post.get("caption_entities")
            if isinstance(entities, list):
                return caption, [e for e in entities if isinstance(e, dict)]
            return caption, []

        return "", []

    @staticmethod
    def media_group_key(post: dict[str, Any]) -> str:
        media_group_id = str(post.get("media_group_id") or "")
        chat_id = BridgeService.extract_tg_chat_id(post)
        if chat_id is None:
            return media_group_id
        return f"{chat_id}:{media_group_id}"

    @staticmethod
    def extract_tg_chat_id(post: dict[str, Any]) -> int | None:
        chat = post.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id")
            if isinstance(chat_id, int):
                return chat_id
        return None

    @staticmethod
    def extract_tg_message_id(post: dict[str, Any]) -> int | None:
        message_id = post.get("message_id")
        if isinstance(message_id, int):
            return message_id
        return None

    @staticmethod
    def extract_reply_to_message_id(post: dict[str, Any]) -> int | None:
        reply = post.get("reply_to_message")
        if isinstance(reply, dict):
            reply_message_id = reply.get("message_id")
            if isinstance(reply_message_id, int):
                return reply_message_id
        return None

    @classmethod
    def split_text_for_max(cls, text: str) -> list[str]:
        text = text.strip()
        if len(text) <= cls.MAX_TEXT_LIMIT:
            return [text]

        parts: list[str] = []
        remaining = text
        suffix = cls.CONTINUATION_SUFFIX
        max_head_len = cls.MAX_TEXT_LIMIT - len(suffix)
        if max_head_len < cls.SPLIT_MIN:
            return [text[: cls.MAX_TEXT_LIMIT]]

        while len(remaining) > cls.MAX_TEXT_LIMIT:
            split_at = cls.choose_split_point(remaining, cls.SPLIT_TARGET, max_head_len)
            head = remaining[:split_at].rstrip()
            tail = remaining[split_at:].lstrip()
            if not head or not tail:
                split_at = max_head_len
                head = remaining[:split_at].rstrip()
                tail = remaining[split_at:].lstrip()

            head = head + suffix
            if len(head) > cls.MAX_TEXT_LIMIT:
                head = head[: cls.MAX_TEXT_LIMIT]
            parts.append(head)
            remaining = tail

        parts.append(remaining)
        return parts

    @classmethod
    def choose_split_point(cls, text: str, target: int, max_len: int) -> int:
        def candidate_points(separator: str) -> list[int]:
            points: list[int] = []
            pos = 0
            while True:
                idx = text.find(separator, pos)
                if idx == -1:
                    break
                point = idx + len(separator)
                if cls.SPLIT_MIN <= point <= max_len:
                    points.append(point)
                pos = idx + len(separator)
            return points

        for separator in ("\n\n", "\n", " "):
            points = candidate_points(separator)
            if points:
                point = min(points, key=lambda x: abs(x - target))
                return cls.safe_html_split_point(text, point, max_len)

        return cls.safe_html_split_point(text, max_len, max_len)

    @classmethod
    def safe_html_split_point(cls, text: str, point: int, max_len: int) -> int:
        point = max(1, min(point, max_len))
        last_open = text.rfind("<", 0, point)
        last_close = text.rfind(">", 0, point)
        if last_open > last_close and last_open >= cls.SPLIT_MIN:
            point = last_open
        return point

    async def publish_to_max(
        self,
        text: str,
        attachments: list[dict[str, Any]],
        post: dict[str, Any],
    ) -> list[str]:
        text_parts = self.split_text_for_max(text)
        reply_to_max_mid = await self.resolve_reply_target_mid(post)

        max_ids: list[str] = []
        for index, part in enumerate(text_parts):
            part_attachments = attachments if index == 0 else []
            reply_mid = reply_to_max_mid if index == 0 else (max_ids[-1] if max_ids else None)
            max_message = await self.send_to_max(
                text=part,
                attachments=part_attachments,
                reply_to_mid=reply_mid,
            )
            max_id = self.extract_max_message_id(max_message)
            if max_id:
                max_ids.append(max_id)
        return max_ids

    async def resolve_existing_max_ids(self, post: dict[str, Any]) -> list[str]:
        tg_chat_id = self.extract_tg_chat_id(post)
        tg_message_id = self.extract_tg_message_id(post)
        if tg_chat_id is None or tg_message_id is None:
            return []

        mapped = self.message_map.get_ids(tg_chat_id, tg_message_id)
        if mapped:
            return mapped

        source_text, _ = self.extract_text_and_entities(post)
        inferred_mid = await self.find_max_mid_by_text(source_text)
        if inferred_mid:
            self.message_map.put_ids(tg_chat_id, tg_message_id, [inferred_mid])
            return [inferred_mid]
        return []

    async def resolve_reply_target_mid(self, post: dict[str, Any]) -> str | None:
        tg_chat_id = self.extract_tg_chat_id(post)
        reply_to_tg_message_id = self.extract_reply_to_message_id(post)
        if tg_chat_id is None or reply_to_tg_message_id is None:
            return None

        mapped = self.message_map.get(tg_chat_id, reply_to_tg_message_id)
        if mapped:
            return mapped

        reply_post = post.get("reply_to_message")
        if not isinstance(reply_post, dict):
            return None

        reply_text, _ = self.extract_text_and_entities(reply_post)
        inferred_mid = await self.find_max_mid_by_text(reply_text)
        if inferred_mid:
            self.message_map.put(tg_chat_id, reply_to_tg_message_id, inferred_mid)
        return inferred_mid

    async def update_existing_max_post(
        self,
        existing_max_ids: list[str],
        text: str,
    ) -> list[str]:
        text_parts = self.split_text_for_max(text)
        if not text_parts:
            text_parts = [""]

        updated_ids = list(existing_max_ids)

        for index, part in enumerate(text_parts):
            if index < len(updated_ids):
                message_id = updated_ids[index]
                await self.edit_max_message(message_id=message_id, text=part)
            else:
                reply_mid = updated_ids[index - 1] if updated_ids else None
                new_message = await self.send_to_max(text=part, attachments=[], reply_to_mid=reply_mid)
                new_id = self.extract_max_message_id(new_message)
                if new_id:
                    updated_ids.append(new_id)

        for stale_id in updated_ids[len(text_parts) :]:
            await self.delete_max_message(stale_id)

        return updated_ids[: len(text_parts)]

    async def edit_max_message(self, message_id: str, text: str) -> None:
        url = f"{self.settings.max_api_base}/messages"
        headers = {
            "Authorization": self.settings.max_bot_token,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": text[: self.MAX_TEXT_LIMIT],
            "format": "html",
        }

        params = {"message_id": message_id}
        max_attempts = 8
        transient_statuses = {429, 500, 502, 503, 504}
        for attempt in range(max_attempts):
            response = await self.http.put(url, headers=headers, params=params, json=payload)
            if response.status_code in transient_statuses:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.2)
                    continue
                response.raise_for_status()
            if response.status_code >= 400:
                response.raise_for_status()
            return
        raise RuntimeError(f"MAX edit failed after retries for {message_id}")

    async def delete_max_message(self, message_id: str) -> None:
        url = f"{self.settings.max_api_base}/messages"
        headers = {"Authorization": self.settings.max_bot_token}
        params = {"message_id": message_id}
        response = await self.http.delete(url, headers=headers, params=params)
        response.raise_for_status()

    async def get_recent_max_messages(self, count: int = 200) -> list[dict[str, Any]]:
        url = f"{self.settings.max_api_base}/messages"
        headers = {"Authorization": self.settings.max_bot_token}
        safe_count = max(1, min(count, 100))
        params = {"chat_id": self.settings.max_target_chat_id, "count": safe_count}
        response = await self.http.get(url, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
        messages = payload.get("messages")
        if isinstance(messages, list):
            return [message for message in messages if isinstance(message, dict)]
        return []

    @staticmethod
    def normalize_match_text(text: str) -> str:
        return " ".join((text or "").lower().split())

    @staticmethod
    def common_prefix_len(left: str, right: str) -> int:
        limit = min(len(left), len(right))
        idx = 0
        while idx < limit and left[idx] == right[idx]:
            idx += 1
        return idx

    @classmethod
    def score_text_match(cls, source: str, candidate: str) -> int:
        if not source or not candidate:
            return 0

        source_n = cls.normalize_match_text(source)
        candidate_n = cls.normalize_match_text(candidate)
        if not source_n or not candidate_n:
            return 0
        if source_n == candidate_n:
            return 5000

        if len(source_n) < 80:
            return 0

        prefix = cls.common_prefix_len(source_n, candidate_n)
        score = prefix
        if prefix >= 120:
            score += 1200

        min_sub_len = 120
        if len(candidate_n) >= min_sub_len and candidate_n in source_n:
            score += 600
        if len(source_n) >= min_sub_len and source_n in candidate_n:
            score += 500

        source_tail = source_n.split(" ")[-1]
        if source_tail.startswith("#") and source_tail in candidate_n:
            score += 120
        return score

    async def find_max_mid_by_text(self, source_text: str) -> str | None:
        source_text = (source_text or "").strip()
        if not source_text:
            return None

        source_n = self.normalize_match_text(source_text)
        if not source_n:
            return None

        messages = await self.get_recent_max_messages(count=250)
        best_score = 0
        best_candidate_text = ""
        best_mid: str | None = None
        for message in messages:
            body = message.get("body")
            if not isinstance(body, dict):
                continue
            candidate_text = body.get("text")
            candidate_mid = body.get("mid")
            if not isinstance(candidate_text, str) or not isinstance(candidate_mid, str):
                continue
            score = self.score_text_match(source_text, candidate_text)
            if score > best_score:
                best_score = score
                best_candidate_text = candidate_text
                best_mid = candidate_mid

        if best_score >= 1200:
            return best_mid
        if best_score >= 650 and best_mid:
            candidate_n = self.normalize_match_text(best_candidate_text)
            prefix_len = min(160, len(source_n), len(candidate_n))
            if prefix_len >= 80 and (
                source_n.startswith(candidate_n[:prefix_len])
                or candidate_n.startswith(source_n[:prefix_len])
            ):
                return best_mid
        return None

    @staticmethod
    def utf16_to_py_index_map(text: str) -> list[int]:
        mapping = [0]
        for py_index, ch in enumerate(text):
            utf16_units = len(ch.encode("utf-16-le")) // 2
            mapping.extend([py_index + 1] * utf16_units)
        return mapping

    @staticmethod
    def entity_markers(
        entity_type: str, entity: dict[str, Any]
    ) -> tuple[str, str, int] | None:
        if entity_type == "text_link":
            url = entity.get("url")
            if isinstance(url, str) and url:
                safe_url = html.escape(url, quote=True)
                return f'<a href="{safe_url}">', "</a>", 10
            return None

        if entity_type == "text_mention":
            user = entity.get("user")
            if isinstance(user, dict) and isinstance(user.get("id"), int):
                return f'<a href="tg://user?id={user["id"]}">', "</a>", 10
            return None

        markers: dict[str, tuple[str, str, int]] = {
            "bold": ("<b>", "</b>", 20),
            "italic": ("<i>", "</i>", 20),
            "underline": ("<u>", "</u>", 20),
            "strikethrough": ("<s>", "</s>", 20),
            "code": ("<code>", "</code>", 5),
        }
        if entity_type in markers:
            return markers[entity_type]

        if entity_type == "pre":
            return "<pre>", "</pre>", 5

        return None

    @classmethod
    def convert_tg_entities_to_html(
        cls, text: str, entities: list[dict[str, Any]]
    ) -> str:
        if not text or not entities:
            return html.escape(text or "")

        index_map = cls.utf16_to_py_index_map(text)
        total_utf16_units = len(index_map) - 1
        normalized: list[dict[str, Any]] = []
        for entity in entities:
            entity_type = entity.get("type")
            offset = entity.get("offset")
            length = entity.get("length")
            if not isinstance(entity_type, str) or not isinstance(offset, int) or not isinstance(length, int):
                continue
            if length <= 0 or offset < 0 or offset + length > total_utf16_units:
                continue

            start = index_map[offset]
            end = index_map[offset + length]
            if start >= end:
                continue

            if entity_type in {"blockquote", "expandable_blockquote"}:
                continue

            marker = cls.entity_markers(entity_type, entity)
            if marker is None:
                continue

            open_marker, close_marker, priority = marker
            normalized.append(
                {
                    "start": start,
                    "end": end,
                    "open": open_marker,
                    "close": close_marker,
                    "priority": priority,
                }
            )

        if not normalized:
            return html.escape(text)

        opens: dict[int, list[dict[str, Any]]] = defaultdict(list)
        closes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in normalized:
            opens[item["start"]].append(item)
            closes[item["end"]].append(item)

        out: list[str] = []
        for idx in range(len(text) + 1):
            if idx in closes:
                for item in sorted(closes[idx], key=lambda x: (-x["start"], x["priority"])):
                    out.append(item["close"])

            if idx < len(text):
                if idx in opens:
                    for item in sorted(opens[idx], key=lambda x: (x["priority"], -x["end"])):
                        out.append(item["open"])
                out.append(html.escape(text[idx]))

        return "".join(out)

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

    async def send_to_max(
        self, text: str, attachments: list[dict[str, Any]], reply_to_mid: str | None = None
    ) -> dict[str, Any]:
        url = f"{self.settings.max_api_base}/messages"
        headers = {
            "Authorization": self.settings.max_bot_token,
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "text": text[: self.MAX_TEXT_LIMIT],
            "format": "html",
        }
        if attachments:
            payload["attachments"] = attachments
        if reply_to_mid:
            payload["link"] = {
                "type": "reply",
                "chat_id": self.settings.max_target_chat_id,
                "mid": reply_to_mid,
            }

        params = {"chat_id": self.settings.max_target_chat_id}
        transient_statuses = {429, 500, 502, 503, 504}
        max_attempts = 10

        for attempt in range(max_attempts):
            response = await self.http.post(url, headers=headers, params=params, json=payload)

            if response.status_code in transient_statuses:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.5)
                    continue
                response.raise_for_status()

            if response.status_code >= 400:
                if self.is_attachment_not_ready(response) and attempt < max_attempts - 1:
                    await asyncio.sleep(4.0)
                    continue
                response.raise_for_status()

            return response.json()

        raise RuntimeError("MAX send failed after retries")

    async def reset_tg_update_delivery(self) -> None:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/setWebhook"
        payload = {
            "url": "",
            "drop_pending_updates": self.settings.tg_polling_drop_pending_updates,
        }
        response = await self.http.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram setWebhook failed: {body}")

    async def fetch_tg_updates(self, offset: int | None) -> list[dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/getUpdates"
        payload: dict[str, Any] = {
            "timeout": self.settings.tg_polling_timeout_sec,
            "allowed_updates": ["channel_post", "edited_channel_post", "message"],
        }
        if offset is not None:
            payload["offset"] = offset

        request_timeout = max(float(self.settings.tg_polling_timeout_sec) + 15.0, 30.0)
        response = await self.http.post(url, json=payload, timeout=request_timeout)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {body}")

        result = body.get("result")
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def get_polling_offset(self) -> int | None:
        raw = self.message_map.get_state(self.TG_POLL_OFFSET_STATE_KEY)
        if raw is None:
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def set_polling_offset(self, offset: int) -> None:
        self.message_map.set_state(self.TG_POLL_OFFSET_STATE_KEY, str(offset))

    @staticmethod
    def is_attachment_not_ready(response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except Exception:
            return False
        return payload.get("code") == "attachment.not.ready"

    async def notify_admin(self, text: str) -> None:
        admin_id = self.settings.tg_admin_id

        try:
            await self.send_tg_message(chat_id=admin_id, text=text[:4000])
        except Exception:
            logging.exception("Failed to notify TG admin")

    async def send_tg_message(self, chat_id: int | str, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        response = await self.http.post(url, json=payload)
        response.raise_for_status()

    @staticmethod
    def extract_max_message_id(response_json: dict[str, Any]) -> str | None:
        message = response_json.get("message")
        if isinstance(message, dict):
            body = message.get("body")
            if isinstance(body, dict) and isinstance(body.get("mid"), str):
                return body["mid"]
            return message.get("message_id") or message.get("id")
        return response_json.get("message_id") or response_json.get("id")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


async def run_polling(settings: Settings) -> None:
    service = BridgeService(settings)
    offset = service.get_polling_offset()
    backoff_seconds = 3.0
    try:
        await service.reset_tg_update_delivery()
        while True:
            try:
                updates = await service.fetch_tg_updates(offset=offset)
                if not updates:
                    continue
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                        service.set_polling_offset(offset)
                    await service.handle_tg_update(update)
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                # Long polling may occasionally exceed transport timeout window.
                continue
            except Exception:
                logging.exception("Polling loop failed, retrying")
                await asyncio.sleep(backoff_seconds)
    finally:
        await service.close()


if __name__ == "__main__":
    load_dotenv()
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    try:
        conf = load_settings()
    except RuntimeError as exc:
        logging.error(
            "Конфигурация невалидна: %s. "
            "Укажите обязательные параметры (включая TG_SOURCE_CHAT_ID и TG_ADMIN_ID) перед запуском.",
            exc,
        )
        raise SystemExit(1)

    asyncio.run(run_polling(conf))
