#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open VK in Playwright, let user sign in manually, and save storage_state "
            "for server-side browser automation."
        )
    )
    parser.add_argument(
        "--storage-state",
        default="vk_storage_state.json",
        help="Path to storage state JSON file (default: vk_storage_state.json).",
    )
    parser.add_argument(
        "--channel",
        default="",
        help="Optional Playwright browser channel (e.g. chrome, msedge).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (not recommended for login flow).",
    )
    parser.add_argument(
        "--start-url",
        default="https://vk.com/",
        help="Initial URL to open.",
    )
    return parser.parse_args()


def safe_dismiss_dialog(dialog: Any) -> None:
    try:
        dialog.dismiss()
    except Exception:
        # Ignore dialog races from driver internals.
        pass


def collect_origins_for_storage_state(context: Any) -> list[dict[str, Any]]:
    by_origin: dict[str, list[dict[str, str]]] = {}
    for page in context.pages:
        try:
            origin = page.evaluate("() => location.origin")
            if not isinstance(origin, str) or not origin.startswith("http"):
                continue
            local_storage = page.evaluate(
                "() => Object.entries(localStorage).map(([name, value]) => ({name, value}))"
            )
            items: list[dict[str, str]] = []
            if isinstance(local_storage, list):
                for entry in local_storage:
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name")
                    value = entry.get("value")
                    if isinstance(name, str) and isinstance(value, str):
                        items.append({"name": name, "value": value})
            by_origin[origin] = items
        except Exception:
            continue

    return [{"origin": origin, "localStorage": items} for origin, items in by_origin.items()]


def save_storage_state_with_fallback(context: Any, storage_state_path: Path) -> str:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            context.storage_state(path=str(storage_state_path))
            return "native"
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))

    cookies = []
    try:
        cookies = context.cookies()
    except Exception:
        pass

    payload = {
        "cookies": cookies if isinstance(cookies, list) else [],
        "origins": collect_origins_for_storage_state(context),
    }
    storage_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if last_exc is not None:
        print(f"Warning: native storage_state failed, fallback JSON written: {last_exc}")
    return "fallback"


def main() -> int:
    args = parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("Playwright is not installed. Install with `pip install playwright`.")
        print("Then run `playwright install chromium`.")
        return 1

    storage_state_path = Path(args.storage_state).expanduser().resolve()
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    launch_kwargs = {"headless": bool(args.headless)}
    if args.channel:
        launch_kwargs["channel"] = args.channel

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)

        context_kwargs = {}
        if storage_state_path.exists():
            context_kwargs["storage_state"] = str(storage_state_path)
        context = browser.new_context(**context_kwargs)
        context.on("dialog", safe_dismiss_dialog)

        page = context.new_page()
        page.on("dialog", safe_dismiss_dialog)
        page.goto(args.start_url, wait_until="domcontentloaded")

        print("\nBrowser opened.")
        print("1) Complete VK login/2FA manually in the opened window.")
        print("2) Navigate to your target group page if needed.")
        print("3) Return here and press Enter.\n")
        input("Press Enter after login is complete: ")

        mode = save_storage_state_with_fallback(context, storage_state_path)
        print(f"Storage state save mode: {mode}")
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    print(f"Saved storage state: {storage_state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
