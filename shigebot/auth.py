"""
shigebot-auth — interactive OAuth authorization code flow.

Run once to generate the bot account's access + refresh token pair.
Prints the four lines ready to paste into your environment file.

Usage:
    shigebot-auth

You will be prompted for your client_id and client_secret (or they can
be set in TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET already).

What it does:
  1. Starts a temporary HTTP server on localhost:18756
  2. Opens the Twitch authorization URL in your browser
  3. Waits for the redirect (you must be logged in as the BOT account)
  4. Exchanges the code for an access + refresh token pair
  5. Prints the env var lines ready to copy into your secrets file
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from typing import NamedTuple

REDIRECT_PORT = 18756
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
SCOPES = "user:read:chat user:write:chat user:bot"

logger = logging.getLogger(__name__)


class TokenPair(NamedTuple):
    access_token: str
    refresh_token: str


# ── Local callback server ─────────────────────────────────────────────────────

class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the single redirect from Twitch after the user authorizes."""

    result: _CallbackResult  # set on the class before the server starts

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            self.result.error = params["error"][0]
        elif "code" in params:
            state = params.get("state", [None])[0]
            if state != self.server.expected_state:  # type: ignore[attr-defined]
                self.result.error = "state_mismatch"
            else:
                self.result.code = params["code"][0]
        else:
            self.result.error = "no_code_or_error"

        body = self._response_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.result.event.set()

    def _response_html(self) -> bytes:
        if self.result.error:
            msg = f"Authorization failed: {self.result.error}"
            colour = "#e05252"
        else:
            msg = "Authorization successful — you can close this tab."
            colour = "#5db85d"
        return f"""<!DOCTYPE html><html><body style="
            font-family:system-ui;display:flex;align-items:center;
            justify-content:center;height:100vh;margin:0;background:#1c1917">
            <p style="color:{colour};font-size:1.4rem">{msg}</p>
        </body></html>""".encode()

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # silence request logging


# ── Token exchange ────────────────────────────────────────────────────────────

def _exchange_code(client_id: str, client_secret: str, code: str) -> TokenPair:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Token exchange failed ({exc.code}): {body}") from exc

    if "access_token" not in payload:
        raise RuntimeError(f"Unexpected response from Twitch: {payload}")

    return TokenPair(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
    )


def _validate_token(access_token: str) -> dict:
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ── Main flow ─────────────────────────────────────────────────────────────────

def run_auth() -> None:
    print("shigebot auth — OAuth authorization code flow")
    print("=" * 50)
    print()

    # Get credentials
    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    if not client_id:
        client_id = input("Client ID    : ").strip()
    else:
        print(f"Client ID    : (from TWITCH_CLIENT_ID)")

    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_secret:
        client_secret = input("Client Secret: ").strip()
    else:
        print(f"Client Secret: (from TWITCH_CLIENT_SECRET)")

    if not client_id or not client_secret:
        print("Error: client_id and client_secret are required.", file=sys.stderr)
        sys.exit(1)

    print()
    print("Make sure you are logged into Twitch in your browser as the BOT")
    print("account before continuing (not your personal/streamer account).")
    print()
    input("Press Enter when ready...")
    print()

    # Build auth URL
    state = secrets.token_urlsafe(16)
    auth_url = (
        "https://id.twitch.tv/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope={urllib.parse.quote(SCOPES)}"
        f"&state={state}"
        f"&force_verify=true"
    )

    # Start callback server
    result = _CallbackResult()
    _CallbackHandler.result = result

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    server.expected_state = state  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    # Open browser
    print(f"Opening browser for Twitch authorization...")
    print(f"If it doesn't open automatically, visit:")
    print(f"  {auth_url}")
    print()
    webbrowser.open(auth_url)

    # Wait for redirect (timeout after 120s)
    if not result.event.wait(timeout=120):
        print("Timed out waiting for authorization. Please try again.", file=sys.stderr)
        sys.exit(1)

    server.server_close()

    if result.error:
        print(f"Authorization failed: {result.error}", file=sys.stderr)
        sys.exit(1)

    print("Authorization received, exchanging code for tokens...")

    try:
        tokens = _exchange_code(client_id, client_secret, result.code)  # type: ignore[arg-type]
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Validate to get the login name for confirmation
    login = "unknown"
    user_id = "unknown"
    try:
        info = _validate_token(tokens.access_token)
        login = info.get("login", "unknown")
        user_id = info.get("user_id", "unknown")
        scopes = info.get("scopes", [])
        print(f"Token issued for: {login} (id: {user_id})")
        print(f"Scopes: {' '.join(scopes)}")
        print()

        missing = {s for s in SCOPES.split() if s not in scopes}
        if missing:
            print(f"WARNING: missing scopes: {missing}", file=sys.stderr)
            print("The bot may not work correctly.", file=sys.stderr)
            print()

    except Exception as exc:
        print(f"Warning: could not validate token: {exc}", file=sys.stderr)
        print()

    # Print result
    print("Add these lines to your environment file (/var/lib/secrets/shigebot-env):")
    print()
    print(f"TWITCH_CLIENT_ID={client_id}")
    print(f"TWITCH_CLIENT_SECRET={client_secret}")
    print(f"TWITCH_BOT_TOKEN={tokens.access_token}")
    print(f"TWITCH_BOT_REFRESH={tokens.refresh_token}")
    print()
    print("Also add bot_id to your shigebot.toml:")
    if user_id != "unknown":
        print(f"  bot_id = \"{user_id}\"")
    else:
        print("  (look up the bot account's numeric ID — see README)")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        run_auth()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
