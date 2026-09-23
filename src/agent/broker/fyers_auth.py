"""FYERS login: obtain today's access token.

FYERS requires an interactive login with 2FA once per trading day. This module:
  1. reuses today's cached token if it still works,
  2. otherwise opens the FYERS login page in the browser,
  3. captures the auth_code from the redirect (automatically when the redirect URI is
     localhost, otherwise by asking you to paste the redirected URL),
  4. exchanges it for an access token and caches it in data/fyers_token.json.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_PATH = PROJECT_ROOT / "data" / "fyers_token.json"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOGIN_TIMEOUT_S = 300


class FyersAuthError(RuntimeError):
    pass


def _load_credentials() -> tuple[str, str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        return (
            os.environ["FYERS_APP_ID"],
            os.environ["FYERS_SECRET_KEY"],
            os.environ["FYERS_REDIRECT_URI"],
        )
    except KeyError as missing:
        raise FyersAuthError(f"{missing} is not set — copy .env.example to .env and fill it in") from None


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _read_cached_token() -> str | None:
    if not TOKEN_PATH.exists():
        return None
    cached = json.loads(TOKEN_PATH.read_text())
    return cached["access_token"] if cached.get("date") == _today() else None


def _write_cached_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps({"date": _today(), "access_token": token}))


def _rest_client(app_id: str, token: str) -> fyersModel.FyersModel:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return fyersModel.FyersModel(client_id=app_id, token=token, is_async=False, log_path=str(LOG_DIR))


def _token_works(app_id: str, token: str) -> bool:
    return _rest_client(app_id, token).get_profile().get("s") == "ok"


def _capture_on_localhost(redirect_uri: str) -> str:
    """Run a one-shot HTTP listener on the redirect URI and return the redirected URL."""
    parsed = urlparse(redirect_uri)
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            is_redirect = "state=" in self.path  # ignore favicon and other stray requests
            if is_redirect:
                captured["url"] = self.path
            self.send_response(200 if is_redirect else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if is_redirect:
                self.wfile.write(b"<h3>FYERS login received. You can close this tab.</h3>")

        def log_message(self, *args):
            pass

    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    try:
        while "url" not in captured:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FyersAuthError("Timed out waiting for the FYERS login redirect")
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    return captured["url"]


def _auth_code_from(url: str, expected_state: str) -> str:
    params = parse_qs(urlparse(url).query)
    if params.get("state", [None])[0] != expected_state:
        raise FyersAuthError("Login redirect had an unexpected state value — try again")
    code = params.get("auth_code", [None])[0]
    if not code:
        raise FyersAuthError(f"No auth_code in redirect: {params}")
    return code


def login() -> tuple[str, str]:
    """Return (app_id, access_token) for today, logging in interactively if needed."""
    app_id, secret_key, redirect_uri = _load_credentials()

    cached = _read_cached_token()
    if cached and _token_works(app_id, cached):
        return app_id, cached

    state = secrets.token_urlsafe(16)
    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
        state=state,
    )
    login_url = session.generate_authcode()
    print("Opening FYERS login in your browser. Complete login + 2FA there.")
    print(f"If the browser doesn't open, visit:\n{login_url}\n")
    webbrowser.open(login_url)

    if urlparse(redirect_uri).hostname in ("127.0.0.1", "localhost"):
        redirected = _capture_on_localhost(redirect_uri)
    else:
        redirected = input("After login, paste the FULL URL from the browser address bar here:\n> ").strip()

    session.set_token(_auth_code_from(redirected, state))
    response = session.generate_token()
    token = response.get("access_token")
    if not token:
        raise FyersAuthError(f"Token exchange failed: {response}")

    _write_cached_token(token)
    return app_id, token


def client() -> fyersModel.FyersModel:
    """A logged-in FYERS REST client for today."""
    return _rest_client(*login())
