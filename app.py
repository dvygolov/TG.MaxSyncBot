from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import sqlite3
import time
import uuid
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
    vk_group_id: int
    vk_enable_edit_sync: bool
    vk_storage_state_path: str
    vk_browser_headless: bool
    vk_browser_channel: str
    vk_browser_timeout_sec: int
    vk_media_tmp_dir: str
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
        vk_group_id=required_int("VK_GROUP_ID"),
        vk_enable_edit_sync=env_bool("VK_ENABLE_EDIT_SYNC", False),
        vk_storage_state_path=(os.getenv("VK_STORAGE_STATE_PATH") or "vk_storage_state.json").strip(),
        vk_browser_headless=env_bool("VK_BROWSER_HEADLESS", True),
        vk_browser_channel=(os.getenv("VK_BROWSER_CHANNEL") or "").strip(),
        vk_browser_timeout_sec=env_int("VK_BROWSER_TIMEOUT_SEC", 60, min_value=10),
        vk_media_tmp_dir=(os.getenv("VK_MEDIA_TMP_DIR") or ".vk_media_tmp").strip(),
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
            CREATE TABLE IF NOT EXISTS tg_vk_message_map (
                tg_chat_id INTEGER NOT NULL,
                tg_message_id INTEGER NOT NULL,
                vk_post_id TEXT NOT NULL,
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
    def serialize_ids(cls, vk_post_ids: list[str]) -> str:
        cleaned = [message_id for message_id in vk_post_ids if message_id]
        return cls.SEPARATOR.join(cleaned)

    @classmethod
    def deserialize_ids(cls, raw_value: str | None) -> list[str]:
        if not raw_value:
            return []
        return [part for part in raw_value.split(cls.SEPARATOR) if part]

    def put_ids(self, tg_chat_id: int, tg_message_id: int, vk_post_ids: list[str]) -> None:
        raw_value = self.serialize_ids(vk_post_ids)
        if not raw_value:
            return
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO tg_vk_message_map (tg_chat_id, tg_message_id, vk_post_id)
                VALUES (?, ?, ?)
                ON CONFLICT(tg_chat_id, tg_message_id) DO UPDATE SET
                    vk_post_id=excluded.vk_post_id,
                    created_at=strftime('%s','now')
                """,
                (tg_chat_id, tg_message_id, raw_value),
            )
            self.conn.commit()

    def put(self, tg_chat_id: int, tg_message_id: int, vk_post_id: str) -> None:
        self.put_ids(tg_chat_id, tg_message_id, [vk_post_id])

    def get_ids(self, tg_chat_id: int, tg_message_id: int) -> list[str]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT vk_post_id
                FROM tg_vk_message_map
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


class VkBrowserPoster:
    VK_API_BASE = "https://api.vk.com/method"
    VK_API_VERSION = "5.269"

    def __init__(self, settings: Settings) -> None:
        self.group_id = abs(settings.vk_group_id)
        self.storage_state_path = Path(settings.vk_storage_state_path)
        self.browser_headless = settings.vk_browser_headless
        self.browser_channel = settings.vk_browser_channel
        self.timeout_ms = settings.vk_browser_timeout_sec * 1000
        self.lock = Lock()

    def post(self, text: str, attachment_paths: list[str]) -> str | None:
        with self.lock:
            return self._post_impl(text=text, attachment_paths=attachment_paths)

    def check_session(self) -> dict[str, Any]:
        with self.lock:
            return self._check_session_impl()

    def _post_impl(self, text: str, attachment_paths: list[str]) -> str | None:
        token_info = self.extract_web_access_token()
        token = token_info["token"]
        with httpx.Client(timeout=120.0) as client:
            attachments = self.upload_attachments_via_api(
                client=client,
                token=token,
                attachment_paths=attachment_paths,
            )
            response = self.vk_api_call(
                client=client,
                method="wall.post",
                token=token,
                owner_id=-self.group_id,
                from_group=1,
                message=text,
                attachments=",".join(attachments) if attachments else "",
                signed=0,
                close_comments=0,
                mute_notifications=0,
            )
        post_id = response.get("post_id")
        return str(post_id) if post_id is not None else None

    def _check_session_impl(self) -> dict[str, Any]:
        try:
            token_info = self.extract_web_access_token()
            token = token_info["token"]
            current_url = token_info.get("url", "")
            with httpx.Client(timeout=60.0) as client:
                self.vk_api_call(
                    client=client,
                    method="groups.getById",
                    token=token,
                    group_ids=str(self.group_id),
                )
                self.vk_api_call(
                    client=client,
                    method="wall.get",
                    token=token,
                    owner_id=-self.group_id,
                    count=1,
                )
            return {
                "ok": True,
                "url": current_url,
                "storage_state_exists": self.storage_state_path.exists(),
                "token_prefix": token[:24],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": ""}

    @staticmethod
    def find_first_visible(
        locator: Any,
        max_items: int = 25,
        min_width: int = 160,
        min_y: int = 80,
        max_y: int | None = None,
    ) -> Any | None:
        count = min(locator.count(), max_items)
        best_item = None
        best_y: float | None = None
        for idx in range(count):
            item = locator.nth(idx)
            try:
                if not item.is_visible():
                    continue
                box = item.bounding_box()
                if not box:
                    continue
                x = float(box.get("x", 0) or 0)
                y = float(box.get("y", 0) or 0)
                width = float(box.get("width", 0) or 0)
                if width < min_width or y < min_y:
                    continue
                if max_y is not None and y > max_y:
                    continue
                # Skip controls hidden near the far-left sidebar.
                if x < 220:
                    continue
                if best_item is None or (best_y is not None and y < best_y) or best_y is None:
                    best_item = item
                    best_y = y
            except Exception:
                continue
        return best_item

    def wait_for_group_ui(self, page: Any) -> None:
        page.wait_for_load_state("domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 7000))
        except Exception:
            # VK keeps long polling connections open; networkidle may timeout.
            pass
        deadline = time.time() + 25
        stable_ticks = 0
        last_signature = ""
        while time.time() < deadline:
            try:
                ready_state = page.evaluate("document.readyState")
            except Exception:
                ready_state = ""
            has_surface = self.has_composer_surface(page)
            join_links = self.count_guest_join_links(page)
            signature = f"{page.url}|{ready_state}|{has_surface}|{join_links}"
            if ready_state == "complete" and (has_surface or join_links > 0):
                if signature == last_signature:
                    stable_ticks += 1
                else:
                    stable_ticks = 1
                    last_signature = signature
                if stable_ticks >= 3:
                    break
            page.wait_for_timeout(1000)

    def count_guest_join_links(self, page: Any) -> int:
        selectors = "a[href='/join'], a[href^='/join?'], a[href*='vk.com/join']"
        try:
            return page.locator(selectors).count()
        except Exception:
            return 0

    def capture_debug_screenshot(self, page: Any, prefix: str) -> str | None:
        try:
            debug_dir = Path(".vk_debug")
            debug_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())
            screenshot_path = debug_dir / f"{prefix}_{timestamp}.png"
            page.screenshot(path=str(screenshot_path), full_page=False)
            return str(screenshot_path)
        except Exception:
            return None

    @staticmethod
    def extract_token_from_text(raw: str | None) -> str | None:
        if not raw:
            return None
        token_match = re.search(r"(vk1\.a\.[A-Za-z0-9_\-]+)", raw)
        if token_match:
            return token_match.group(1)
        token_match = re.search(r"access_token=([^&\"'\\s]+)", raw)
        if token_match:
            return token_match.group(1)
        return None

    def extract_web_access_token(self) -> dict[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(f"Playwright is not installed: {exc}") from exc

        launch_kwargs: dict[str, Any] = {"headless": self.browser_headless}
        if self.browser_channel:
            launch_kwargs["channel"] = self.browser_channel

        captured_tokens: list[str] = []
        token_sources: dict[str, str] = {}
        debug_screenshot: str | None = None

        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_kwargs)
                context_kwargs: dict[str, Any] = {}
                if self.storage_state_path.exists():
                    context_kwargs["storage_state"] = str(self.storage_state_path)
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)

                def on_request(request: Any) -> None:
                    if "vk.com" not in (request.url or ""):
                        return
                    token = self.extract_token_from_text(request.url)
                    if not token:
                        token = self.extract_token_from_text(getattr(request, "post_data", None))
                    if token:
                        if token not in captured_tokens:
                            captured_tokens.append(token)
                        token_sources[token] = request.url or ""

                context.on("request", on_request)

                probe_urls = [
                    f"https://vk.com/club{self.group_id}",
                    "https://vk.com/feed",
                    "https://vk.com/im",
                ]
                for probe_url in probe_urls:
                    page.goto(probe_url, wait_until="domcontentloaded")
                    self.wait_for_group_ui(page)
                    page.wait_for_timeout(2000)

                self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(self.storage_state_path))
                if not captured_tokens:
                    debug_screenshot = self.capture_debug_screenshot(page, "vk_token_extract_failed")
                    raise RuntimeError(
                        "Unable to capture VK web access token from browser session. "
                        "Refresh session with vk_session_refresh.py."
                    )

                with httpx.Client(timeout=45.0) as client:
                    for token in reversed(captured_tokens):
                        try:
                            self.vk_api_call(client=client, method="users.get", token=token)
                            return {"token": token, "url": token_sources.get(token, "")}
                        except Exception:
                            continue

                debug_screenshot = self.capture_debug_screenshot(page, "vk_token_invalid")
                raise RuntimeError(
                    "Unable to find a valid VK web access token in captured browser requests."
                )
        finally:
            if page is not None and debug_screenshot is not None:
                logging.warning("VK token extraction failed screenshot: %s", debug_screenshot)
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

    def vk_api_call(
        self,
        client: httpx.Client,
        method: str,
        token: str,
        **params: Any,
    ) -> Any:
        endpoint = f"{self.VK_API_BASE}/{method}"
        payload: dict[str, Any] = {
            "access_token": token,
            "v": self.VK_API_VERSION,
        }
        for key, value in params.items():
            if value is None:
                continue
            payload[key] = value

        for attempt in range(5):
            response = client.post(endpoint, data=payload)
            response.raise_for_status()
            data = response.json()
            error = data.get("error")
            if not error:
                return data.get("response")

            code = error.get("error_code")
            message = error.get("error_msg") or "Unknown VK API error"
            if code == 6 and attempt < 4:
                time.sleep(0.45)
                continue
            raise RuntimeError(f"VK API {method} failed: code={code} message={message}")
        raise RuntimeError(f"VK API {method} failed after retries.")

    def upload_attachments_via_api(
        self,
        client: httpx.Client,
        token: str,
        attachment_paths: list[str],
    ) -> list[str]:
        attachments: list[str] = []
        for path in attachment_paths:
            file_path = Path(path)
            mime = (mimetypes.guess_type(str(file_path))[0] or "").lower()
            if mime.startswith("image/"):
                attachments.append(self.upload_photo_via_api(client=client, token=token, path=file_path))
                continue
            if mime.startswith("video/"):
                attachments.append(self.upload_video_via_api(client=client, token=token, path=file_path))
                continue
            raise RuntimeError(
                f"Unsupported VK attachment type for API posting: {file_path.name} ({mime or 'unknown mime'})"
            )
        return attachments

    def upload_photo_via_api(self, client: httpx.Client, token: str, path: Path) -> str:
        server = self.vk_api_call(
            client=client,
            method="photos.getWallUploadServer",
            token=token,
            group_id=self.group_id,
        )
        upload_url = server.get("upload_url")
        if not upload_url:
            raise RuntimeError("VK API photos.getWallUploadServer returned no upload_url.")

        with path.open("rb") as file_handle:
            upload_response = client.post(
                upload_url,
                files={"photo": (path.name, file_handle, mimetypes.guess_type(str(path))[0] or "image/jpeg")},
            )
        upload_response.raise_for_status()
        upload_data = upload_response.json()

        saved = self.vk_api_call(
            client=client,
            method="photos.saveWallPhoto",
            token=token,
            group_id=self.group_id,
            photo=upload_data.get("photo"),
            server=upload_data.get("server"),
            hash=upload_data.get("hash"),
        )
        if not isinstance(saved, list) or not saved:
            raise RuntimeError("VK API photos.saveWallPhoto returned empty response.")
        item = saved[0]
        owner_id = item.get("owner_id")
        media_id = item.get("id")
        if owner_id is None or media_id is None:
            raise RuntimeError("VK API photos.saveWallPhoto returned invalid media identifiers.")
        return f"photo{owner_id}_{media_id}"

    def upload_video_via_api(self, client: httpx.Client, token: str, path: Path) -> str:
        video_info = self.vk_api_call(
            client=client,
            method="video.save",
            token=token,
            group_id=self.group_id,
            wallpost=1,
            name=path.stem,
        )
        upload_url = video_info.get("upload_url")
        owner_id = video_info.get("owner_id")
        video_id = video_info.get("video_id")
        if not upload_url or owner_id is None or video_id is None:
            raise RuntimeError("VK API video.save returned incomplete upload data.")

        with path.open("rb") as file_handle:
            upload_response = client.post(
                upload_url,
                files={"video_file": (path.name, file_handle, mimetypes.guess_type(str(path))[0] or "video/mp4")},
                timeout=600.0,
            )
        upload_response.raise_for_status()
        return f"video{owner_id}_{video_id}"

    def has_composer_surface(self, page: Any) -> bool:
        selectors = [
            "#submit_post_box",
            "#post_field_wrap",
            ".submit_post_box",
            ".post_field_wrap",
            ".wall_post_box",
            ".wall_module .post_write",
            ".wall_create_post",
            "#page_add_media",
        ]
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return self.find_editor(page) is not None

    def find_composer_trigger(self, page: Any) -> Any | None:
        selectors = [
            "#submit_post_box button",
            "#submit_post_box [role='button']",
            "#post_field_wrap button",
            "#post_field_wrap [role='button']",
            ".submit_post_box button",
            ".submit_post_box [role='button']",
            ".post_field_wrap button",
            ".post_field_wrap [role='button']",
            ".wall_post_box button",
            ".wall_post_box [role='button']",
            "[data-testid*='post_create']",
            "[data-testid*='create_post']",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            target = self.find_first_visible(locator, max_items=30, min_width=80, max_y=900)
            if target is not None:
                return target
        return None

    def ensure_logged_in(self, page: Any) -> None:
        current_url = page.url or ""
        normalized_url = current_url.lower()
        if "login.vk.com" in normalized_url or "id.vk.com/auth" in normalized_url:
            raise RuntimeError(
                "VK session is not authorized (redirected to login). "
                "Refresh session with vk_session_refresh.py."
            )

        join_links = self.count_guest_join_links(page)
        if join_links > 0 and not self.has_composer_surface(page):
            raise RuntimeError(
                "VK session is not authorized. Refresh session with vk_session_refresh.py."
            )

    def open_post_editor(self, page: Any) -> None:
        editor = self.find_editor(page)
        if editor is not None:
            editor.click()
            return

        if self.discover_composer_surface(page, try_open=True):
            editor = self.find_editor(page)
            if editor is not None:
                editor.click()
                return
        raise RuntimeError("Cannot find VK post editor on page.")

    def can_open_post_editor(self, page: Any) -> bool:
        if self.has_composer_surface(page):
            return True

        if self.discover_composer_surface(page, try_open=True):
            return True
        return False

    def discover_composer_surface(self, page: Any, try_open: bool) -> bool:
        for _ in range(5):
            if self.has_composer_surface(page):
                return True

            trigger = self.find_composer_trigger(page)
            if trigger is not None and try_open:
                try:
                    trigger.click()
                    page.wait_for_timeout(600)
                    if self.has_composer_surface(page):
                        return True
                except Exception:
                    pass

            try:
                page.keyboard.press("PageDown")
            except Exception:
                try:
                    page.mouse.wheel(0, 900)
                except Exception:
                    pass
            page.wait_for_timeout(750)
        return self.has_composer_surface(page)

    def find_editor(self, page: Any) -> Any | None:
        selectors = [
            "#post_field",
            "#post_field_wrap div[contenteditable='true']",
            ".post_field_wrap div[contenteditable='true']",
            ".submit_post_box div[contenteditable='true']",
            ".wall_post_box div[contenteditable='true']",
            "div[role='textbox'][contenteditable='true']",
            "div[contenteditable='true'][data-placeholder]",
            "div[contenteditable='true']",
            "textarea",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            target = self.find_first_visible(locator)
            if target is not None:
                return target
        return None

    def fill_post_text(self, page: Any, text: str) -> None:
        editor = self.find_editor(page)
        if editor is None:
            raise RuntimeError("Cannot focus VK post text field.")

        editor.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        if text:
            page.keyboard.type(text)

    def attach_files(self, page: Any, attachment_paths: list[str]) -> None:
        attach_buttons = [
            r"Фото|Фотография|Видео|Файл|Attach|Photo|Video|File",
            r"Добавить|Add",
        ]
        for pattern in attach_buttons:
            locator = page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
            target = self.find_first_visible(locator, max_items=10)
            if target is not None:
                try:
                    target.click()
                    page.wait_for_timeout(200)
                except Exception:
                    pass

        input_selectors = [
            ".box_layer input[type='file']",
            ".post_field input[type='file']",
            ".wall_module input[type='file']",
            "input[type='file']",
        ]
        input_target = None
        for selector in input_selectors:
            locator = page.locator(selector)
            count = locator.count()
            if count <= 0:
                continue
            input_target = locator.nth(count - 1)
            break
        if input_target is None:
            raise RuntimeError("Cannot find file input for VK attachments.")

        input_target.set_input_files(attachment_paths)
        page.wait_for_timeout(1500)

    def submit_post(self, page: Any) -> None:
        submit_patterns = [
            r"Опубликовать|Отправить|Publish|Post",
        ]
        for pattern in submit_patterns:
            locator = page.get_by_role("button", name=re.compile(pattern, re.IGNORECASE))
            target = self.find_first_visible(locator, max_items=15)
            if target is not None:
                target.click()
                page.wait_for_timeout(1200)
                return

        # Fallback hotkey used by many VK post boxes.
        page.keyboard.press("Control+Enter")
        page.wait_for_timeout(1200)

    def find_top_post_id(self, page: Any) -> str | None:
        hrefs = page.eval_on_selector_all(
            f"a[href*='/wall-{self.group_id}_']",
            "els => els.map(el => el.getAttribute('href') || '')",
        )
        best: int | None = None
        pattern = re.compile(rf"/wall-{self.group_id}_(\d+)")
        for href in hrefs:
            if not isinstance(href, str):
                continue
            match = pattern.search(href)
            if not match:
                continue
            try:
                post_id = int(match.group(1))
            except Exception:
                continue
            if best is None or post_id > best:
                best = post_id
        return str(best) if best is not None else None

    def wait_for_new_post_id(self, page: Any, before_post_id: str | None) -> str | None:
        before_num: int | None = None
        if before_post_id and before_post_id.isdigit():
            before_num = int(before_post_id)

        deadline = time.time() + 25
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            page.reload(wait_until="domcontentloaded")
            current_post_id = self.find_top_post_id(page)
            if not current_post_id:
                continue
            if before_num is None:
                return current_post_id
            if current_post_id.isdigit() and int(current_post_id) > before_num:
                return current_post_id
        return None


class BridgeService:
    VK_TEXT_LIMIT = 16000
    SPLIT_TARGET = 12000
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
        self.vk_poster = VkBrowserPoster(settings)
        self.media_tmp_dir = Path(settings.vk_media_tmp_dir)
        self.media_tmp_dir.mkdir(parents=True, exist_ok=True)

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
        if command not in {"/start", "/status", "/vk_session", "/check_session"}:
            return

        if not self.is_admin_message(message):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        if command in {"/start", "/status"}:
            response_text = (
                "Привет, админ. Я работаю.\n"
                f"Источник TG: {self.settings.tg_source_chat_id}\n"
                f"Назначение VK (group_id): {abs(self.settings.vk_group_id)}\n"
                f"Session state: {self.settings.vk_storage_state_path}\n"
                f"Session file exists: {'yes' if Path(self.settings.vk_storage_state_path).exists() else 'no'}"
            )
            await self.send_tg_message(chat_id=chat_id, text=response_text)
            return

        session_status = await asyncio.to_thread(self.vk_poster.check_session)
        if session_status.get("ok"):
            response_text = (
                "VK session check: OK\n"
                f"url={session_status.get('url')}\n"
                f"token_prefix={session_status.get('token_prefix')}\n"
                f"storage_state_exists={session_status.get('storage_state_exists')}"
            )
            await self.send_tg_message(chat_id=chat_id, text=response_text)
            return

        response_text = (
            "VK session check: ERROR\n"
            f"url={session_status.get('url')}\n"
            f"error={session_status.get('error')}\n"
            f"screenshot={session_status.get('screenshot')}"
        )
        await self.send_tg_message(chat_id=chat_id, text=response_text[:4000])

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
            text = self.convert_tg_entities_to_vk_text(source_text, entities).strip()
            attachments = await self.build_vk_attachments(post)
            if not text and not attachments:
                return
            vk_post_ids = await self.publish_to_vk(
                text=text,
                attachments=attachments,
            )
            vk_post_id = vk_post_ids[0] if vk_post_ids else None
            if vk_post_id and tg_chat_id is not None and tg_message_id is not None:
                self.message_map.put_ids(tg_chat_id, tg_message_id, vk_post_ids)
            await self.notify_admin(
                f"OK: post {tg_message_id} published to VK"
                + (f" (vk_post_id={vk_post_id})" if vk_post_id else "")
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
        attachments: list[str] = []

        try:
            text = self.convert_tg_entities_to_vk_text(source_text, source_entities).strip()
            for post in posts:
                attachments.extend(await self.build_vk_attachments(post))

            vk_post_ids = await self.publish_to_vk(
                text=text,
                attachments=attachments,
            )
            vk_post_id = vk_post_ids[0] if vk_post_ids else None
            if vk_post_id and tg_chat_id is not None:
                for post in posts:
                    tg_message_id = self.extract_tg_message_id(post)
                    if tg_message_id is not None:
                        self.message_map.put_ids(tg_chat_id, tg_message_id, vk_post_ids)
            await self.notify_admin(
                f"OK: media group lead {lead_message_id} ({len(posts)} items) published to VK"
                + (f" (vk_post_id={vk_post_id})" if vk_post_id else "")
            )
        except Exception as exc:
            logging.exception("Failed to publish media group lead %s", lead_message_id)
            self.cleanup_media_files(attachments)
            await self.notify_admin(
                f"ERROR: media group lead {lead_message_id} ({len(posts)} items) failed: {exc}"
            )

    async def process_edited_post(self, post: dict[str, Any]) -> None:
        if not self.settings.vk_enable_edit_sync:
            return
        await self.notify_admin(
            "WARN: edited_channel_post received, but edit sync is not supported in browser posting mode."
        )
        return

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
    def vk_group_id_abs(group_id: int) -> int:
        return abs(group_id)

    @classmethod
    def split_text_for_vk(cls, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= cls.VK_TEXT_LIMIT:
            return [text]

        parts: list[str] = []
        remaining = text
        suffix = cls.CONTINUATION_SUFFIX
        max_head_len = cls.VK_TEXT_LIMIT - len(suffix)
        if max_head_len < cls.SPLIT_MIN:
            return [text[: cls.VK_TEXT_LIMIT]]

        while len(remaining) > cls.VK_TEXT_LIMIT:
            split_at = cls.choose_split_point(remaining, cls.SPLIT_TARGET, max_head_len)
            head = remaining[:split_at].rstrip()
            tail = remaining[split_at:].lstrip()
            if not head or not tail:
                split_at = max_head_len
                head = remaining[:split_at].rstrip()
                tail = remaining[split_at:].lstrip()

            head = head + suffix
            if len(head) > cls.VK_TEXT_LIMIT:
                head = head[: cls.VK_TEXT_LIMIT]
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
                return min(points, key=lambda x: abs(x - target))

        return max_len

    async def publish_to_vk(
        self,
        text: str,
        attachments: list[str],
    ) -> list[str]:
        attachment_files = [item for item in attachments if item]
        try:
            text_parts = self.split_text_for_vk(text)
            if not text_parts and attachment_files:
                text_parts = [""]
            if not text_parts:
                return []

            attachment_batches = self.split_attachment_batches_for_vk(attachment_files)
            vk_post_ids: list[str] = []
            for index, part in enumerate(text_parts):
                part_attachments = attachment_batches[0] if index == 0 else []
                vk_post_id = await self.send_to_vk(
                    text=part,
                    attachment_ids=part_attachments,
                )
                if vk_post_id:
                    vk_post_ids.append(vk_post_id)

            for batch_index, extra_attachments in enumerate(attachment_batches[1:], start=2):
                extra_text = f"[attachment {batch_index}/{len(attachment_batches)}]"
                vk_post_id = await self.send_to_vk(text=extra_text, attachment_ids=extra_attachments)
                if vk_post_id:
                    vk_post_ids.append(vk_post_id)
            return vk_post_ids
        finally:
            self.cleanup_media_files(attachment_files)

    @classmethod
    def split_attachment_batches_for_vk(cls, attachments: list[str]) -> list[list[str]]:
        cleaned = [item for item in attachments if item]
        if not cleaned:
            return [[]]

        batches: list[list[str]] = []
        current: list[str] = []
        for attachment in cleaned:
            current.append(attachment)
            if len(current) >= 10:
                batches.append(current)
                current = []

        if current:
            batches.append(current)

        return batches or [[]]

    @staticmethod
    def normalize_match_text(text: str) -> str:
        return " ".join((text or "").lower().split())

    @staticmethod
    def utf16_to_py_index_map(text: str) -> list[int]:
        mapping = [0]
        for py_index, ch in enumerate(text):
            utf16_units = len(ch.encode("utf-16-le")) // 2
            mapping.extend([py_index + 1] * utf16_units)
        return mapping

    @classmethod
    def convert_tg_entities_to_vk_text(cls, text: str, entities: list[dict[str, Any]]) -> str:
        if not text:
            return ""
        if not entities:
            return text

        index_map = cls.utf16_to_py_index_map(text)
        total_utf16_units = len(index_map) - 1
        inserts: dict[int, list[str]] = {}
        quote_ranges: list[tuple[int, int]] = []

        for entity in entities:
            entity_type = entity.get("type")
            offset = entity.get("offset")
            length = entity.get("length")
            if (
                not isinstance(offset, int)
                or not isinstance(length, int)
                or length <= 0
            ):
                continue
            if offset < 0 or offset + length > total_utf16_units:
                continue

            start = index_map[offset]
            end = index_map[offset + length]
            if start >= end:
                continue

            if entity_type in {"blockquote", "expandable_blockquote", "quote"}:
                quote_ranges.append((start, end))
                continue

            if entity_type != "text_link":
                continue

            url = entity.get("url")
            if not isinstance(url, str):
                continue
            link = url.strip()
            if not link:
                continue
            anchor = text[start:end].strip()
            if anchor and cls.normalize_match_text(anchor) == cls.normalize_match_text(link):
                continue
            inserts.setdefault(end, []).append(f" ({link})")

        if quote_ranges:
            quote_ranges.sort(key=lambda r: (r[0], r[1]))
            merged_ranges: list[tuple[int, int]] = []
            for start, end in quote_ranges:
                if not merged_ranges:
                    merged_ranges.append((start, end))
                    continue
                prev_start, prev_end = merged_ranges[-1]
                if start > prev_end:
                    merged_ranges.append((start, end))
                else:
                    merged_ranges[-1] = (prev_start, max(prev_end, end))
            quote_ranges = merged_ranges

        if not inserts and not quote_ranges:
            return text

        out: list[str] = []
        range_idx = 0
        for idx, ch in enumerate(text):
            while range_idx < len(quote_ranges) and idx >= quote_ranges[range_idx][1]:
                range_idx += 1
            if range_idx < len(quote_ranges):
                start, end = quote_ranges[range_idx]
                if start <= idx < end and (idx == start or text[idx - 1] == "\n"):
                    out.append(">> ")
            out.append(ch)
            suffixes = inserts.get(idx + 1)
            if suffixes:
                out.extend(suffixes)
        return "".join(out)

    async def build_vk_attachments(self, post: dict[str, Any]) -> list[str]:
        media = self.extract_media(post)
        if media is None:
            return []

        file_id, media_kind, fallback_name, mime_type = media
        file_bytes, file_name = await self.download_tg_file(file_id)
        if not file_name:
            file_name = fallback_name
        if not mime_type:
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        local_path = self.save_media_file(
            file_bytes=file_bytes,
            file_name=file_name,
            media_kind=media_kind,
            mime_type=mime_type,
        )
        return [local_path]

    def save_media_file(
        self,
        file_bytes: bytes,
        file_name: str,
        media_kind: str,
        mime_type: str,
    ) -> str:
        source = Path(file_name).name or "attachment.bin"
        suffix = Path(source).suffix
        if not suffix:
            guessed_ext = mimetypes.guess_extension(mime_type or "")
            suffix = guessed_ext or ".bin"

        name_prefix = media_kind if media_kind in {"photo", "video", "doc"} else "media"
        output_name = f"{name_prefix}_{uuid.uuid4().hex}{suffix}"
        output_path = self.media_tmp_dir / output_name
        output_path.write_bytes(file_bytes)
        return str(output_path)

    @staticmethod
    def cleanup_media_files(paths: list[str]) -> None:
        for raw_path in paths:
            if not raw_path:
                continue
            try:
                file_path = Path(raw_path)
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                logging.warning("Failed to cleanup temp media file: %s", raw_path)

    def extract_media(self, post: dict[str, Any]) -> tuple[str, str, str, str] | None:
        photos = post.get("photo")
        if isinstance(photos, list) and photos:
            last_photo = photos[-1]
            file_id = last_photo.get("file_id")
            if file_id:
                return file_id, "photo", "photo.jpg", "image/jpeg"

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

        document = post.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            name = document.get("file_name") or "document.bin"
            mime_type = document.get("mime_type") or "application/octet-stream"
            return document["file_id"], "doc", name, mime_type

        audio = post.get("audio")
        if isinstance(audio, dict) and audio.get("file_id"):
            name = audio.get("file_name") or "audio.mp3"
            mime_type = audio.get("mime_type") or "audio/mpeg"
            return audio["file_id"], "doc", name, mime_type

        voice = post.get("voice")
        if isinstance(voice, dict) and voice.get("file_id"):
            name = "voice.ogg"
            mime_type = voice.get("mime_type") or "audio/ogg"
            return voice["file_id"], "doc", name, mime_type

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

    async def send_to_vk(self, text: str, attachment_ids: list[str]) -> str | None:
        attachments = [item for item in attachment_ids if item]
        if len(attachments) > 10:
            logging.warning("VK web UI supports up to 10 attachments per post. Trimming %s -> 10.", len(attachments))
            attachments = attachments[:10]

        safe_text = (text or "")[: self.VK_TEXT_LIMIT]
        return await asyncio.to_thread(self.vk_poster.post, safe_text, attachments)

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
