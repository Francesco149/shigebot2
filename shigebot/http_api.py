"""
shigebot/http_api.py — minimal HTTP API for external message injection.

Provides a single POST /inject endpoint that lets external tools (OBS scripts,
browser extensions, local apps) send messages into the bot's processing
pipeline as if they came from chat.

Configuration (shigebot.toml):

    [bot]
    http_api_port = 8765   # 0 or omit to disable

Secret (environment):

    SHIGEBOT_HTTP_SECRET=your-secret-here

Request format:

    POST /inject HTTP/1.1
    Authorization: Bearer <secret>
    Content-Type: application/json

    {
        "channel":        "mychannel",   # required
        "user":           "alice",        # required
        "message":        "!lurk hello", # required
        "is_mod":         false,          # optional, default false
        "is_broadcaster": false           # optional, default false
    }

Response:

    200 OK          → {"ok": true}
    400 Bad Request → {"error": "..."}
    401 Unauthorized → {"error": "unauthorized"}
    405 Method Not Allowed → {"error": "method not allowed"}

Security notes:
- Bind to 127.0.0.1 only — not exposed to the network.
- Secret is read from SHIGEBOT_HTTP_SECRET env var.
- Injected messages run through the full command/ambient pipeline including
  operator checks and group checks, but with is_operator/is_mod reflecting
  what the caller specifies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot import Shigebot

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """
    Read one HTTP/1.1 request from `reader`.
    Returns (method, path, headers, body) or None on connection error.
    """
    try:
        # Request line
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not line:
            return None
        parts = line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            return None
        method, path = parts[0].upper(), parts[1]

        # Headers
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line or line in (b"\r\n", b"\n"):
                break
            if b":" in line:
                name, _, value = line.decode("utf-8", errors="replace").partition(":")
                headers[name.strip().lower()] = value.strip()

        # Body
        content_length = int(headers.get("content-length", 0))
        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(
                reader.readexactly(min(content_length, 65536)), timeout=5.0
            )

        return method, path, headers, body

    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        return None


def _http_response(status: int, body: dict) -> bytes:
    status_text = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                   405: "Method Not Allowed"}.get(status, "Error")
    payload = json.dumps(body).encode()
    return (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + payload


async def _handle_connection(
    reader:    asyncio.StreamReader,
    writer:    asyncio.StreamWriter,
    bot:       "Shigebot",
    secret:    str,
) -> None:
    try:
        result = await _read_http_request(reader)
        if result is None:
            return

        method, path, headers, body = result

        if path != "/inject":
            writer.write(_http_response(404, {"error": "not found"}))
            return

        if method != "POST":
            writer.write(_http_response(405, {"error": "method not allowed"}))
            return

        # Auth
        auth = headers.get("authorization", "")
        if not secret or not auth == f"Bearer {secret}":
            writer.write(_http_response(401, {"error": "unauthorized"}))
            return

        # Parse body
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            writer.write(_http_response(400, {"error": "invalid JSON"}))
            return

        channel = data.get("channel", "").strip()
        user    = data.get("user", "").strip().lower()
        message = data.get("message", "").strip()

        if not channel or not user or not message:
            writer.write(_http_response(
                400, {"error": "channel, user, and message are required"}
            ))
            return

        is_mod         = bool(data.get("is_mod", False))
        is_broadcaster = bool(data.get("is_broadcaster", False))

        # Inject into the bot's pipeline
        ok = await bot.inject_message(
            channel        = channel,
            user           = user,
            message        = message,
            is_mod         = is_mod,
            is_broadcaster = is_broadcaster,
        )

        if ok:
            writer.write(_http_response(200, {"ok": True}))
        else:
            writer.write(_http_response(400, {"error": f"unknown channel: {channel!r}"}))

    except Exception as exc:
        logger.error("HTTP API handler error: %s", exc, exc_info=True)
        try:
            writer.write(_http_response(500, {"error": "internal error"}))
        except Exception:
            pass
    finally:
        try:
            await writer.drain()
            writer.close()
        except Exception:
            pass


async def serve(bot: "Shigebot", port: int) -> None:
    """
    Start the HTTP API server and serve until cancelled.

    Called from __main__._run_once() as an asyncio task.
    """
    secret = os.environ.get("SHIGEBOT_HTTP_SECRET", "").strip()
    if not secret:
        logger.warning(
            "HTTP API is enabled (port %d) but SHIGEBOT_HTTP_SECRET is not set — "
            "all requests will be rejected.",
            port,
        )

    server = await asyncio.start_server(
        lambda r, w: _handle_connection(r, w, bot, secret),
        host = _HOST,
        port = port,
    )

    addr = server.sockets[0].getsockname() if server.sockets else (_HOST, port)
    logger.info("HTTP API listening on %s:%d", addr[0], addr[1])

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        logger.info("HTTP API server stopped")
        raise
