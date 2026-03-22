"""
Shigebot: twitchio v3 bot using EventSub over WebSocket.

Auth model (v3)
---------------
v3 dropped IRC entirely. Chat is read via EventSub (channel.chat.message)
and written via the Twitch Send Message API. This requires:

  Client credentials (registered Twitch application):
    TWITCH_CLIENT_ID     — from dev.twitch.tv
    TWITCH_CLIENT_SECRET — from dev.twitch.tv

  Bot identity:
    bot_id in shigebot.toml — numeric Twitch user ID of the bot account
                              Get it: https://api.twitch.tv/helix/users?login=<botname>

  User OAuth token for the bot account (scopes required):
    user:read:chat    — read chat messages
    user:write:chat   — send chat messages
    user:bot          — identify as a bot

  TWITCH_BOT_TOKEN    — access token
  TWITCH_BOT_REFRESH  — refresh token (twitchio manages rotation automatically)

  How to generate the initial token pair:
    Use the authorization code flow — NOT `twitch token -u` (device flow)
    which does not issue a refresh token. Without a refresh token the bot
    silently dies after ~4 hours when the access token expires.
    See the README for the curl-based two-step flow.

Elevation detection
-------------------
v3 includes badge data on every ChatMessage event. We read
message.chatter.moderator / .vip / .broadcaster on each incoming message
and update the elevated_channels set live. No USERSTATE polling needed.

Duplicate message prevention
-----------------------------
Same Chatterino approach as before — double a space or append U+034F.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import twitchio
from twitchio.ext import commands
from twitchio import eventsub

from .config import Config
from .gist import GistManager
from .ratelimit import RateLimiter
from .runner import ScriptRunner

logger = logging.getLogger(__name__)

MAGIC_SUFFIX = " \u034f"


def deduplicate(text: str, last_sent: str) -> str:
    """
    Mutate ``text`` just enough to avoid Twitch's duplicate-message filter.
    No-op if ``text != last_sent``.
    """
    if text != last_sent:
        return text

    ignore_first_space = bool(text) and text[0] in (".", "/")
    space_idx = text.find(" ")
    if ignore_first_space and space_idx != -1:
        space_idx = text.find(" ", space_idx + 1)

    if space_idx == -1:
        return text + MAGIC_SUFFIX
    return text[:space_idx] + "  " + text[space_idx + 1:]


class Shigebot(commands.Bot):
    def __init__(self, config: Config, gist_manager: GistManager) -> None:
        super().__init__(
            client_id=config.get_client_id(),
            client_secret=config.get_client_secret(),
            bot_id=config.bot.bot_id,
            prefix=config.bot.prefix,
        )
        self.cfg = config
        self.gist_manager = gist_manager
        self.runner = ScriptRunner(
            working_dir=config.bot.working_dir,
            timeout=float(config.bot.script_timeout),
            extra_preamble=config.bot.script_preamble,
        )

        # Channels where the bot is mod/VIP/broadcaster — updated on every
        # incoming message, shared by reference with the rate limiter.
        self._elevated_channels: set[str] = set()

        # Per-channel last-sent text for duplicate prevention.
        self._last_sent: dict[str, str] = {}

        # Per-user sliding window for !refresh — keyed by username.
        # Tracks timestamps of recent refresh invocations.
        self._refresh_timestamps: dict[str, deque[float]] = {}

        self.rate_limiter = RateLimiter(
            window=config.bot.rate_limit_window,
            non_elevated_max=config.bot.rate_limit_non_elevated_max,
            elevated_max=config.bot.rate_limit_elevated_max,
            elevated_channels=self._elevated_channels,
        )

    # ------------------------------------------------------------------ #
    # Token management
    # ------------------------------------------------------------------ #

    async def load_tokens(self, path: str | None = None) -> None:
        """
        Override default file-based token loading.
        We load the token and refresh token from environment variables
        so they never touch the filesystem.
        """
        token, refresh = self.cfg.get_bot_token_pair()
        await self.add_token(token, refresh)
        logger.info("Bot token loaded from environment")

    async def save_tokens(self, path: str | None = None) -> None:
        """
        Override default file-based token saving.
        twitchio rotates tokens automatically in memory; we don't need
        to persist them because the env vars stay authoritative.
        If you want persistence across restarts without re-generating
        tokens, override this to write to a secrets manager.
        """
        logger.debug("save_tokens called — no-op (tokens live in environment)")

    # ------------------------------------------------------------------ #
    # Setup: subscribe to chat for every configured channel
    # ------------------------------------------------------------------ #

    async def setup_hook(self) -> None:
        """
        Called after login but before the client is ready.
        Subscribe to channel.chat.message for every configured channel.
        """
        for channel_name in self.cfg.all_channels():
            # We need the broadcaster's numeric ID.
            # fetch_users returns a list; take the first match.
            users = await self.fetch_users(logins=[channel_name])
            if not users:
                logger.error(
                    "Could not resolve channel %r to a Twitch user ID — "
                    "skipping subscription. Check the channel name in config.",
                    channel_name,
                )
                continue

            broadcaster = users[0]
            subscription = eventsub.ChatMessageSubscription(
                broadcaster_user_id=broadcaster.id,
                user_id=self.cfg.bot.bot_id,
            )
            await self.subscribe_websocket(subscription, as_bot=True)
            logger.info(
                "Subscribed to chat in #%s (broadcaster_id=%s)",
                channel_name, broadcaster.id,
            )

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    async def event_ready(self) -> None:
        logger.info("Connected and ready | bot_id=%s", self.bot_id)
        channels = list(self.cfg.all_channels())
        logger.info("Subscribed to %d channel(s): %s", len(channels), channels)

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        # Ignore the bot's own messages
        if payload.chatter.id == self.bot_id:
            return

        content = payload.text
        if not content:
            return

        channel_name = payload.broadcaster.name
        username = payload.chatter.name

        ambient = self.cfg.ambient_commands_for_channel(channel_name)

        for command_name in ambient:
            if not self.gist_manager.script_exists(command_name):
                continue

            # fire-and-forget: no await, no blocking, no refresh
            asyncio.create_task(
                self._run_ambient(
                    payload,
                    command_name,
                    channel_name,
                    username,
                    content,
                ),
                name=f"ambient:{command_name}",
            )

        prefix = self.cfg.bot.prefix
        if not content.startswith(prefix):
            return

        parts = content[len(prefix):].split()
        if not parts:
            return

        command_name = parts[0].lower().replace("\u034f", "").strip()
        if not command_name:
            return

        args = [a.replace("\u034f", "").strip() for a in parts[1:]]
        args = [a for a in args if a]

        channel_name = payload.broadcaster.name
        username = payload.chatter.name

        logger.debug(
            "Parsed command: name=%r channel=%r user=%r args=%r",
            command_name, channel_name, username, args,
        )

        allowed = self.cfg.commands_for_channel(channel_name)

        # ── Built-in commands (bypass the allow-list) ─────────────────
        if command_name == "refresh":
            await self._handle_refresh(payload, channel_name, username, args)
            return

        # ── Community script commands ──────────────────────────────────
        if command_name not in allowed:
            logger.debug(
                "Command %r not in allow-list for #%s (allowed: %s)",
                command_name, channel_name, allowed,
            )
            return

        if not self.gist_manager.script_exists(command_name):
            logger.warning(
                "!%s called in #%s but script not yet downloaded",
                command_name, channel_name,
            )
            return

        # Opportunistically refresh the script in the background — don't await,
        # so it never adds latency to the command. If the script was updated,
        # a follow-up message will suggest re-running the command.
        asyncio.create_task(
            self._auto_refresh(payload, command_name),
            name=f"auto-refresh:{command_name}",
        )

        # Update elevation status from badge data on this message
        elevated = payload.chatter.moderator or payload.chatter.vip or payload.chatter.broadcaster
        was_elevated = channel_name in self._elevated_channels
        if elevated:
            self._elevated_channels.add(channel_name)
        else:
            self._elevated_channels.discard(channel_name)
        if elevated != was_elevated:
            status = "elevated" if elevated else "regular"
            logger.info("Elevation change in #%s: %s is now %s", channel_name, username, status)

        logger.info(
            "[#%s|%s] <%s> !%s %s",
            channel_name,
            "elevated" if self.rate_limiter.is_elevated(channel_name) else "regular",
            username,
            command_name,
            " ".join(args),
        )

        async for line in await self.runner.run(
            script_name=command_name,
            channel=channel_name,
            username=username,
            reply_to_user=payload.reply.parent_user.name if payload.reply else "",
            reply_to_message=payload.reply.parent_message_body if payload.reply else "",
            reply_to_message_id=payload.reply.parent_message_id if payload.reply else "",
            msg_id=payload.id,
            timestamp=payload.timestamp.isoformat(),
            prefix = self.cfg.bot.prefix,
            bot_nick = self.cfg.bot.nick,
            args=args,
        ):
            await self._send(payload, line)

    async def _auto_refresh(self, payload: twitchio.ChatMessage, script_name: str) -> None:
        """
        Background task: refresh a single script and notify chat if it changed.

        Rate-limited via a bot-internal key (never clashes with real usernames)
        using the same budget as !refresh. Silently skips if budget is exhausted
        so a busy channel never causes API spam.

        If the script was updated, sends a message suggesting the user re-run
        the command since they just ran the stale version.
        """

        bot_key = f"\x00bot:{self.cfg.bot.nick}"
        limit = self.cfg.bot.refresh_user_limit
        window = self.cfg.bot.refresh_user_window

        now = time.monotonic()
        ts = self._refresh_timestamps.setdefault(bot_key, deque())
        while ts and now - ts[0] >= window:
            ts.popleft()

        if len(ts) >= limit:
            logger.debug(
                "Auto-refresh of %r skipped: bot refresh budget exhausted (%d/%d)",
                script_name, len(ts), limit,
            )
            return

        ts.append(now)
        url = self.gist_manager.scripts.get(script_name)
        if not url:
            return

        self.gist_manager._updated_at.pop(script_name, None)
        try:
            updated = await self.gist_manager.fetch_one(script_name, url)
        except Exception as exc:
            logger.error("Auto-refresh of %r failed: %s", script_name, exc)
            return

        if updated:
            logger.info("Auto-refreshed script %r — notifying chat", script_name)
            prefix = self.cfg.bot.prefix
            await self._send(
                payload,
                f"⟳ {script_name} was just updated — "
                f"run {prefix}{script_name} again for the latest version",
            )

    async def _run_ambient(
        self,
        payload: twitchio.ChatMessage,
        command_name: str,
        channel_name: str,
        username: str,
        content: str,
    ) -> None:
        try:
            async for line in await self.runner.run(
                script_name=command_name,
                channel=channel_name,
                username=username,
                reply_to_user=payload.reply.parent_user.name if payload.reply else "",
                reply_to_message=payload.reply.parent_message_body if payload.reply else "",
                reply_to_message_id=payload.reply.parent_message_id if payload.reply else "",
                msg_id=payload.id,
                timestamp=payload.timestamp.isoformat(),
                prefix = self.cfg.bot.prefix,
                bot_nick = self.cfg.bot.nick,
                args=content.split(),  # full message as args
            ):
                # still respect send pipeline (rate limit + dedup)
                await self._send(payload, line)

        except Exception as exc:
            logger.error(
                "Ambient command %r failed in #%s: %s",
                command_name,
                channel_name,
                exc,
            )

    async def _send(self, payload: twitchio.ChatMessage, text: str) -> None:
        """Send a message to chat, applying dedup and rate limiting."""
        channel_name = payload.broadcaster.name
        text = deduplicate(text, self._last_sent.get(channel_name, ""))
        self._last_sent[channel_name] = text
        await self.rate_limiter.wait_and_send(
            channel_name,
            payload.broadcaster.send_message(text, sender=self.bot_id, token_for=self.bot_id),
        )

    async def _handle_refresh(
        self,
        payload: twitchio.ChatMessage,
        channel_name: str,
        username: str,
        args: list[str],
    ) -> None:
        """
        Force an immediate gist refresh. Available to all users, subject to
        a per-user sliding window (refresh_user_limit / refresh_user_window).

        Usage:
            !refresh          -- refresh all scripts
            !refresh <name>   -- refresh a single script by name
        """

        limit = self.cfg.bot.refresh_user_limit
        window = self.cfg.bot.refresh_user_window

        now = time.monotonic()
        ts = self._refresh_timestamps.setdefault(username, deque())
        while ts and now - ts[0] >= window:
            ts.popleft()

        if len(ts) >= limit:
            wait = int(window - (now - ts[0])) + 1
            logger.info(
                "[#%s] <%s> !refresh rate limited (%d/%d in %.0fs)",
                channel_name, username, len(ts), limit, window,
            )
            reply = (
                f"@{username} slow down -- "
                f"!refresh is limited to {limit} uses per {int(window)}s "
                f"(try again in ~{wait}s)"
            )
            await self._send(payload, reply)
            return

        ts.append(now)

        if args:
            target = args[0].lower()
            if target not in self.gist_manager.scripts:
                reply = f"@{username} unknown script: {target}"
            else:
                self.gist_manager._updated_at.pop(target, None)
                updated = await self.gist_manager.fetch_one(
                    target, self.gist_manager.scripts[target]
                )
                reply = f"@{username} {'updated' if updated else 'already up to date'}: {target}"
                logger.info(
                    "[#%s] !refresh %s by <%s>: updated=%s",
                    channel_name, target, username, updated,
                )
        else:
            logger.info("[#%s] !refresh (all) triggered by <%s>", channel_name, username)
            self.gist_manager._updated_at.clear()
            results = await self.gist_manager.fetch_all()
            changed = [n for n, updated in results.items() if updated]
            if changed:
                reply = f"@{username} updated: {', '.join(sorted(changed))}"
            else:
                reply = f"@{username} all scripts already up to date"

        await self._send(payload, reply)

    async def event_error(self, payload: twitchio.EventErrorPayload) -> None:
        logger.error(
            "twitchio error in %s: %s",
            payload.listener,
            payload.error,
            exc_info=payload.error,
        )
