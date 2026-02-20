#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import sqlite3
import string
import sys
import tempfile
import threading
import time
import webbrowser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

AUTH_ENDPOINT = "https://id.vk.com/authorize"
TOKEN_ENDPOINT = "https://id.vk.com/oauth2/auth"
DEFAULT_SCOPES = "wall photos video docs offline"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_code_verifier(length: int = 64) -> str:
    if length < 43 or length > 128:
        raise ValueError("PKCE code_verifier length must be in [43, 128]")
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def make_code_challenge(code_verifier: str) -> str:
    return b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())


def parse_redirect_url(url: str) -> dict[str, str]:
    parsed = urlparse(url.strip())
    query_data = parse_qs(parsed.query, keep_blank_values=True)
    fragment_data = parse_qs(parsed.fragment, keep_blank_values=True)
    merged: dict[str, str] = {}

    for source in (query_data, fragment_data):
        for key, values in source.items():
            if values:
                merged[key] = values[0]
    return merged


def build_auth_url(
    *,
    client_id: int,
    redirect_uri: str,
    scopes: str,
    response_mode: str,
    response_type: str,
    state: str,
    code_verifier: str,
) -> str:
    code_challenge = make_code_challenge(code_verifier)
    scope = " ".join(scopes.split())
    params = {
        "response_type": response_type,
        "client_id": str(client_id),
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "response_mode": response_mode,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_token(
    *,
    client_id: int,
    redirect_uri: str,
    code_verifier: str,
    code: str | None,
    device_id: str | None,
    state: str | None,
    client_secret: str | None,
    timeout: float,
) -> int:
    if not code:
        print("Error: missing `code` (pass --code or --from-url).", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "client_id": str(client_id),
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }

    if client_secret:
        payload["client_secret"] = client_secret
        if state:
            payload["state"] = state
    else:
        if not device_id:
            print(
                "Error: missing `device_id` for public client flow "
                "(pass --device-id or --from-url containing #device_id=...).",
                file=sys.stderr,
            )
            return 2
        payload["device_id"] = device_id

    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data=payload,
            timeout=timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except Exception as exc:  # pragma: no cover
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except Exception:
        print(response.text)
        return 1 if response.is_error else 0

    print(json.dumps(data, ensure_ascii=False, indent=2))
    if response.is_error:
        return 1

    access_token = data.get("access_token")
    if access_token:
        print()
        print("Put this into .env:")
        print(f"VK_ACCESS_TOKEN={access_token}")
    return 0


def cmd_auth_url(args: argparse.Namespace) -> int:
    if args.code_verifier and not 43 <= len(args.code_verifier) <= 128:
        print("Error: --code-verifier length must be in [43, 128].", file=sys.stderr)
        return 2

    code_verifier = args.code_verifier or generate_code_verifier()
    state = args.state or secrets.token_urlsafe(24)
    auth_url = build_auth_url(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes,
        response_mode=args.response_mode,
        response_type=args.response_type,
        state=state,
        code_verifier=code_verifier,
    )

    print("1) Open this URL in your browser:")
    print(auth_url)
    print()
    print("2) Save these values for token exchange:")
    print(f"code_verifier={code_verifier}")
    print(f"state={state}")
    print()
    print("3) After login/consent, copy the full redirected URL and run `exchange`.")
    return 0


def cmd_exchange(args: argparse.Namespace) -> int:
    redirect_data: dict[str, str] = {}
    if args.from_url:
        redirect_data = parse_redirect_url(args.from_url)

    code = args.code or redirect_data.get("code") or redirect_data.get("code_v2")
    device_id = args.device_id or redirect_data.get("device_id")
    state = args.state or redirect_data.get("state")

    return exchange_code_for_token(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        code_verifier=args.code_verifier,
        code=code,
        device_id=device_id,
        state=state,
        client_secret=args.client_secret,
        timeout=args.timeout,
    )


def cmd_wizard(args: argparse.Namespace) -> int:
    if args.code_verifier and not 43 <= len(args.code_verifier) <= 128:
        print("Error: --code-verifier length must be in [43, 128].", file=sys.stderr)
        return 2

    code_verifier = args.code_verifier or generate_code_verifier()
    state = args.state or secrets.token_urlsafe(24)

    auth_url = build_auth_url(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes,
        response_mode=args.response_mode,
        response_type=args.response_type,
        state=state,
        code_verifier=code_verifier,
    )

    print("Open this URL in your browser:")
    print(auth_url)
    print()
    print("After login/consent, copy the FULL redirected URL and paste it below.")
    print("Press Enter on empty input to cancel.")

    redirected_url = input("> ").strip()
    if not redirected_url:
        print("Cancelled.")
        return 2

    redirect_data = parse_redirect_url(redirected_url)
    redirect_state = redirect_data.get("state")
    if redirect_state and redirect_state != state:
        print("Warning: state mismatch. Check that you used the URL from this wizard.")

    code = redirect_data.get("code") or redirect_data.get("code_v2")
    device_id = redirect_data.get("device_id")

    return exchange_code_for_token(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        code_verifier=code_verifier,
        code=code,
        device_id=device_id,
        state=redirect_state or state,
        client_secret=args.client_secret,
        timeout=args.timeout,
    )


def cmd_auto(args: argparse.Namespace) -> int:
    if args.code_verifier and not 43 <= len(args.code_verifier) <= 128:
        print("Error: --code-verifier length must be in [43, 128].", file=sys.stderr)
        return 2

    parsed_redirect = urlparse(args.redirect_uri)
    if parsed_redirect.scheme not in ("http", "https"):
        print("Error: auto mode supports only http:// or https:// redirect_uri.", file=sys.stderr)
        return 2
    callback_path = parsed_redirect.path or "/"

    bind_host = args.listen_host
    bind_port = args.listen_port
    if bind_port is None:
        if parsed_redirect.hostname in ("localhost", "127.0.0.1"):
            bind_port = parsed_redirect.port or (80 if parsed_redirect.scheme == "http" else 443)
        else:
            bind_port = 8080

    code_verifier = args.code_verifier or generate_code_verifier()
    state = args.state or secrets.token_urlsafe(24)
    auth_url = build_auth_url(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes,
        response_mode="query",
        response_type=args.response_type,
        state=state,
        code_verifier=code_verifier,
    )

    callback_data: dict[str, Any] = {"query": None, "done": threading.Event()}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # type: ignore[override]
            parsed = urlparse(self.path)
            if parsed.path != callback_path:
                body = b"Not found\n"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            callback_data["query"] = {k: v[0] for k, v in query.items() if v}
            callback_data["done"].set()
            body = (
                "VK token helper: callback received. "
                "You can close this tab and return to terminal.\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    try:
        server = http.server.ThreadingHTTPServer((bind_host, bind_port), CallbackHandler)
    except OSError as exc:
        print(
            "Error: cannot bind local callback server. "
            "Try setting --listen-port to a free port (e.g. 8080).",
            file=sys.stderr,
        )
        print(f"Details: {exc}", file=sys.stderr)
        return 1
    server.timeout = 0.5
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Authorization URL:")
    print(auth_url)
    print()
    if args.open_browser:
        opened = webbrowser.open(auth_url, new=1, autoraise=True)
        print(f"Browser open requested: {'ok' if opened else 'failed'}")
    else:
        print("Open browser yourself and authorize.")
    print(f"Waiting for callback on http://{bind_host}:{bind_port}{callback_path} ...")

    started = time.monotonic()
    try:
        while not callback_data["done"].wait(timeout=0.2):
            if time.monotonic() - started > args.wait_timeout:
                print("Error: timeout waiting for redirect callback.", file=sys.stderr)
                return 1
    finally:
        server.shutdown()
        server.server_close()

    query = callback_data["query"] or {}
    callback_state = query.get("state")
    if callback_state and callback_state != state:
        print("Warning: state mismatch. Check application settings and flow.")

    code = query.get("code") or query.get("code_v2")
    device_id = query.get("device_id")

    safe_url = urlunparse(
        (
            parsed_redirect.scheme,
            parsed_redirect.netloc,
            parsed_redirect.path,
            "",
            urlencode({k: v for k, v in query.items() if k != "ext_id"}),
            "",
        )
    )
    print(f"Callback received: {safe_url}")
    return exchange_code_for_token(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        code_verifier=code_verifier,
        code=code,
        device_id=device_id,
        state=callback_state or state,
        client_secret=args.client_secret,
        timeout=args.timeout,
    )


def cmd_playwright_auto(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print(
            "Error: playwright is not available. Install with `pip install playwright`.",
            file=sys.stderr,
        )
        return 1

    if args.code_verifier and not 43 <= len(args.code_verifier) <= 128:
        print("Error: --code-verifier length must be in [43, 128].", file=sys.stderr)
        return 2

    code_verifier = args.code_verifier or generate_code_verifier()
    state = args.state or secrets.token_urlsafe(24)
    auth_url = build_auth_url(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes,
        response_mode="query",
        response_type=args.response_type,
        state=state,
        code_verifier=code_verifier,
    )

    print("Opening Playwright browser for VK login/consent...")
    print("Authorization URL:")
    print(auth_url)
    print()
    print("Complete login in the opened browser window. Waiting for redirect...")

    deadline = time.monotonic() + args.wait_timeout
    redirected_data: dict[str, str] | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(auth_url, wait_until="domcontentloaded")

        while time.monotonic() < deadline and redirected_data is None:
            pages = list(context.pages)
            for pg in pages:
                current_url = pg.url
                if not current_url:
                    continue
                if current_url.startswith(args.redirect_uri):
                    data = parse_redirect_url(current_url)
                    if data.get("code") or data.get("code_v2"):
                        redirected_data = data
                        safe = {k: v for k, v in data.items() if k != "ext_id"}
                        print(f"Redirect detected: {args.redirect_uri}?{urlencode(safe)}")
                        break
            time.sleep(0.25)

        if redirected_data is None:
            print("Error: timeout waiting for redirect URL in browser.", file=sys.stderr)
            browser.close()
            return 1

        browser.close()

    redirect_state = redirected_data.get("state")
    if redirect_state and redirect_state != state:
        print("Warning: state mismatch. Check application settings and flow.")

    return exchange_code_for_token(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        code_verifier=code_verifier,
        code=redirected_data.get("code") or redirected_data.get("code_v2"),
        device_id=redirected_data.get("device_id"),
        state=redirect_state or state,
        client_secret=args.client_secret,
        timeout=args.timeout,
    )


def collect_history_candidates() -> list[str]:
    local = os.getenv("LOCALAPPDATA") or ""
    roaming = os.getenv("APPDATA") or ""
    candidates: list[str] = []

    if local:
        roots = [
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
            os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            os.path.join(local, "Vivaldi", "User Data"),
            os.path.join(local, "Yandex", "YandexBrowser", "User Data"),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for profile in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4"):
                path = os.path.join(root, profile, "History")
                if os.path.isfile(path):
                    candidates.append(path)

    if roaming:
        opera_paths = [
            os.path.join(roaming, "Opera Software", "Opera Stable", "History"),
            os.path.join(roaming, "Opera Software", "Opera GX Stable", "History"),
        ]
        for path in opera_paths:
            if os.path.isfile(path):
                candidates.append(path)

    return sorted(set(candidates))


def current_chromium_ts() -> int:
    # Chromium visit_time: microseconds since 1601-01-01 UTC.
    return int((time.time() + 11644473600) * 1_000_000)


def find_redirect_in_history(
    *,
    redirect_uri_prefix: str,
    expected_state: str | None,
    started_at_ts: int,
    timeout: float,
) -> str | None:
    deadline = time.monotonic() + timeout
    candidates = collect_history_candidates()
    if not candidates:
        return None

    while time.monotonic() < deadline:
        for history_path in candidates:
            if not os.path.isfile(history_path):
                continue
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(prefix="vk_hist_", suffix=".sqlite")
                os.close(fd)
                shutil.copy2(history_path, tmp_path)
                conn = sqlite3.connect(tmp_path)
                try:
                    rows = conn.execute(
                        """
                        SELECT urls.url, visits.visit_time
                        FROM visits
                        JOIN urls ON visits.url = urls.id
                        WHERE urls.url LIKE ?
                        ORDER BY visits.visit_time DESC
                        LIMIT 30
                        """,
                        (f"{redirect_uri_prefix}%",),
                    ).fetchall()
                finally:
                    conn.close()

                for url, visit_time in rows:
                    if int(visit_time) < started_at_ts:
                        continue
                    data = parse_redirect_url(url)
                    code = data.get("code") or data.get("code_v2")
                    if not code:
                        continue
                    state = data.get("state")
                    if expected_state and state and state != expected_state:
                        continue
                    return url
            except Exception:
                pass
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        time.sleep(0.5)
    return None


def cmd_browser_auto(args: argparse.Namespace) -> int:
    if args.code_verifier and not 43 <= len(args.code_verifier) <= 128:
        print("Error: --code-verifier length must be in [43, 128].", file=sys.stderr)
        return 2

    code_verifier = args.code_verifier or generate_code_verifier()
    state = args.state or secrets.token_urlsafe(24)
    auth_url = build_auth_url(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scopes=args.scopes,
        response_mode="query",
        response_type=args.response_type,
        state=state,
        code_verifier=code_verifier,
    )

    started_at = current_chromium_ts()
    print("Opening default browser for VK login/consent...")
    print("Authorization URL:")
    print(auth_url)
    print()
    opened = webbrowser.open(auth_url, new=1, autoraise=True)
    print(f"Browser open requested: {'ok' if opened else 'failed'}")
    print("Waiting for redirect URL in browser history...")

    redirected = find_redirect_in_history(
        redirect_uri_prefix=args.redirect_uri,
        expected_state=state,
        started_at_ts=started_at,
        timeout=args.wait_timeout,
    )
    if not redirected:
        print(
            "Error: timeout waiting for redirect URL in browser history.",
            file=sys.stderr,
        )
        return 1

    data = parse_redirect_url(redirected)
    safe = {k: v for k, v in data.items() if k != "ext_id"}
    print(f"Redirect detected: {args.redirect_uri}?{urlencode(safe)}")
    return exchange_code_for_token(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        code_verifier=code_verifier,
        code=data.get("code") or data.get("code_v2"),
        device_id=data.get("device_id"),
        state=data.get("state") or state,
        client_secret=args.client_secret,
        timeout=args.timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VK ID OAuth helper for access token flow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_url = subparsers.add_parser("auth-url", help="Generate authorization URL with PKCE.")
    auth_url.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    auth_url.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    auth_url.add_argument("--scopes", default=DEFAULT_SCOPES, help="Space-separated scopes.")
    auth_url.add_argument("--state", help="Optional state value.")
    auth_url.add_argument("--code-verifier", help="Optional PKCE verifier (43..128 chars).")
    auth_url.add_argument(
        "--response-mode",
        choices=("fragment", "query"),
        default="fragment",
        help="How VK returns code/device_id in redirect URL.",
    )
    auth_url.add_argument(
        "--response-type",
        choices=("code", "code_v2"),
        default="code",
        help="Authorization response type.",
    )
    auth_url.set_defaults(func=cmd_auth_url)

    exchange = subparsers.add_parser("exchange", help="Exchange code for access_token.")
    exchange.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    exchange.add_argument("--client-secret", help="Protected key for confidential web client.")
    exchange.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    exchange.add_argument("--code", help="Authorization code.")
    exchange.add_argument("--device-id", help="device_id for public client flow.")
    exchange.add_argument(
        "--code-verifier",
        required=True,
        help="PKCE code_verifier used in auth-url step.",
    )
    exchange.add_argument("--state", help="state from auth redirect.")
    exchange.add_argument(
        "--from-url",
        help="Full redirected URL (will parse code/device_id/state automatically).",
    )
    exchange.add_argument("--timeout", type=float, default=30.0)
    exchange.set_defaults(func=cmd_exchange)

    wizard = subparsers.add_parser(
        "wizard",
        help="Interactive one-shot flow: generate URL, wait for redirect URL, exchange code.",
    )
    wizard.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    wizard.add_argument("--client-secret", help="Protected key for confidential web client.")
    wizard.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    wizard.add_argument("--scopes", default=DEFAULT_SCOPES, help="Space-separated scopes.")
    wizard.add_argument("--state", help="Optional state value.")
    wizard.add_argument("--code-verifier", help="Optional PKCE verifier (43..128 chars).")
    wizard.add_argument(
        "--response-mode",
        choices=("fragment", "query"),
        default="query",
        help="How VK returns code/device_id in redirect URL.",
    )
    wizard.add_argument(
        "--response-type",
        choices=("code", "code_v2"),
        default="code",
        help="Authorization response type.",
    )
    wizard.add_argument("--timeout", type=float, default=30.0)
    wizard.set_defaults(func=cmd_wizard)

    auto = subparsers.add_parser(
        "auto",
        help=(
            "Fully automatic local flow: open browser, receive localhost callback, "
            "exchange code for token."
        ),
    )
    auto.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    auto.add_argument("--client-secret", help="Protected key for confidential web client.")
    auto.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    auto.add_argument("--scopes", default=DEFAULT_SCOPES, help="Space-separated scopes.")
    auto.add_argument("--state", help="Optional state value.")
    auto.add_argument("--code-verifier", help="Optional PKCE verifier (43..128 chars).")
    auto.add_argument(
        "--response-type",
        choices=("code", "code_v2"),
        default="code",
        help="Authorization response type.",
    )
    auto.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open browser automatically (default: true).",
    )
    auto.add_argument(
        "--wait-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for localhost callback.",
    )
    auto.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="Local interface for callback listener (default: 127.0.0.1).",
    )
    auto.add_argument(
        "--listen-port",
        type=int,
        help=(
            "Local port for callback listener. "
            "If omitted and redirect host is localhost, uses redirect port; otherwise 8080."
        ),
    )
    auto.add_argument("--timeout", type=float, default=30.0)
    auto.set_defaults(func=cmd_auto)

    pwa = subparsers.add_parser(
        "playwright-auto",
        help=(
            "Automatic browser flow via Playwright: open auth page, wait for redirect URL, "
            "exchange code for token."
        ),
    )
    pwa.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    pwa.add_argument("--client-secret", help="Protected key for confidential web client.")
    pwa.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    pwa.add_argument("--scopes", default=DEFAULT_SCOPES, help="Space-separated scopes.")
    pwa.add_argument("--state", help="Optional state value.")
    pwa.add_argument("--code-verifier", help="Optional PKCE verifier (43..128 chars).")
    pwa.add_argument(
        "--response-type",
        choices=("code", "code_v2"),
        default="code",
        help="Authorization response type.",
    )
    pwa.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run browser headless (default: false).",
    )
    pwa.add_argument(
        "--wait-timeout",
        type=float,
        default=420.0,
        help="Seconds to wait for redirect URL in browser.",
    )
    pwa.add_argument("--timeout", type=float, default=30.0)
    pwa.set_defaults(func=cmd_playwright_auto)

    bwa = subparsers.add_parser(
        "browser-auto",
        help=(
            "Automatic flow in default browser: open auth URL, detect localhost redirect URL "
            "in browser history, exchange code for token."
        ),
    )
    bwa.add_argument("--client-id", required=True, type=int, help="VK application ID.")
    bwa.add_argument("--client-secret", help="Protected key for confidential web client.")
    bwa.add_argument("--redirect-uri", required=True, help="Redirect URI from app settings.")
    bwa.add_argument("--scopes", default=DEFAULT_SCOPES, help="Space-separated scopes.")
    bwa.add_argument("--state", help="Optional state value.")
    bwa.add_argument("--code-verifier", help="Optional PKCE verifier (43..128 chars).")
    bwa.add_argument(
        "--response-type",
        choices=("code", "code_v2"),
        default="code",
        help="Authorization response type.",
    )
    bwa.add_argument(
        "--wait-timeout",
        type=float,
        default=420.0,
        help="Seconds to wait for redirect URL in browser history.",
    )
    bwa.add_argument("--timeout", type=float, default=30.0)
    bwa.set_defaults(func=cmd_browser_auto)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
