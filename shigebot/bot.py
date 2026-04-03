"""
shigebot/bot.py — twitchio v3 bot. (SPEC 2.2)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import AsyncGenerator, Literal

import twitchio
from twitchio import eventsub
from twitchio.ext import commands

from .config import Config
from .context import JobContext
from .gist import GistManager
from .names import filename_to_name, name_to_filename
from .ratelimit import RateLimiter
from .runner import ScriptRunner
from .worker_manager import Action, ChatLine, WorkerManager, is_v2

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

MAGIC_SUFFIX   = " \u034f"
_CHANNEL_DB    = "channel.db"
_WATCHDOG_POLL = 60.0

_ANNOUNCE_COLORS = frozenset({"blue", "green", "orange", "purple", "primary"})

_KV_DDL = """
CREATE TABLE IF NOT EXISTS kv (
    namespace  TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
);
"""


# ── Dedup ──────────────────────────────────────────────────────────────────

def _deduplicate(text: str, last: str) -> str:
    if text != last:
        return text
    is_cmd = bool(text) and text[0] in (".", "/")
    idx = text.find(" ")
    if is_cmd and idx != -1:
        idx = text.find(" ", idx + 1)
    return text + MAGIC_SUFFIX if idx == -1 else text[:idx] + "  " + text[idx + 1:]


# ── Bot ────────────────────────────────────────────────────────────────────

class Shigebot(commands.Bot):

    def __init__(
        self,
        config:         Config,
        gist_manager:   GistManager,
        worker_manager: WorkerManager,
    ) -> None:
        super().__init__(
            client_id     = config.get_client_id(),
            client_secret = config.get_client_secret(),
            bot_id        = config.bot.bot_id,
            prefix        = config.bot.prefix,
        )
        self.cfg            = config
        self.gist_manager   = gist_manager
        self.worker_manager = worker_manager
        self.runner         = ScriptRunner(
            working_dir    = config.bot.working_dir,
            timeout        = float(config.bot.script_timeout),
            extra_preamble = config.bot.script_preamble,
        )

        self._elevated_channels:  set[str]                        = set()
        self._last_sent:          dict[str, str]                  = {}
        self._refresh_timestamps: dict[str, deque[float]]         = {}
        self._broadcasters:       dict[str, twitchio.PartialUser] = {}
        self._last_event_at:      float                           = time.monotonic()

        self.rate_limiter = RateLimiter(
            window            = config.bot.rate_limit_window,
            non_elevated_max  = config.bot.rate_limit_non_elevated_max,
            elevated_max      = config.bot.rate_limit_elevated_max,
            elevated_channels = self._elevated_channels,
        )

    # ── Tokens ────────────────────────────────────────────────────────────

    async def load_tokens(self, path: str | None = None) -> None:
        token, refresh = self.cfg.get_bot_token_pair()
        await self.add_token(token, refresh)
        logger.info("Bot token loaded from environment")

    async def save_tokens(self, path: str | None = None) -> None:
        logger.debug("save_tokens: no-op (tokens live in environment)")

    # ── Setup ─────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        triggers = self.cfg.triggers

        for channel_name in self.cfg.all_channels():
            users = await self.fetch_users(logins=[channel_name])
            if not users:
                logger.error("Cannot resolve channel %r — skipping.", channel_name)
                continue

            broadcaster = users[0]
            self._broadcasters[channel_name] = broadcaster
            bid = broadcaster.id

            # Chat — always
            await self.subscribe_websocket(
                eventsub.ChatMessageSubscription(
                    broadcaster_user_id=bid, user_id=self.cfg.bot.bot_id
                ),
                as_bot=True,
            )
            logger.info("Subscribed to chat in #%s (id=%s)", channel_name, bid)

            # Stream online / offline
            if "stream.online" in triggers:
                await self.subscribe_websocket(
                    eventsub.StreamOnlineSubscription(broadcaster_user_id=bid),
                    as_bot=True,
                )
            if "stream.offline" in triggers:
                await self.subscribe_websocket(
                    eventsub.StreamOfflineSubscription(broadcaster_user_id=bid),
                    as_bot=True,
                )

            # Channel follow (requires moderator:read:followers scope)
            if "channel.follow" in triggers:
                await self.subscribe_websocket(
                    eventsub.ChannelFollowSubscription(
                        broadcaster_user_id=bid,
                        moderator_user_id=self.cfg.bot.bot_id,
                    ),
                    as_bot=True,
                )

            # Incoming raids
            if "channel.raid" in triggers:
                await self.subscribe_websocket(
                    eventsub.ChannelRaidSubscription(
                        to_broadcaster_user_id=bid
                    ),
                    as_bot=True,
                )

            # Ad break (requires channel:read:ads scope)
            if "channel.ad_break" in triggers:
                await self.subscribe_websocket(
                    eventsub.ChannelAdBreakBeginSubscription(
                        broadcaster_user_id=bid
                    ),
                    as_bot=True,
                )

        if self.cfg.bot.watchdog_timeout > 0:
            asyncio.create_task(self._watchdog(), name="watchdog")

    # ── Events ────────────────────────────────────────────────────────────

    async def event_ready(self) -> None:
        self._last_event_at = time.monotonic()
        logger.info("Ready | bot_id=%s | channels=%s", self.bot_id, self.cfg.all_channels())

    async def event_message(self, payload: twitchio.ChatMessage) -> None:
        self._last_event_at = time.monotonic()

        if payload.chatter.id == self.bot_id:
            return

        content      = payload.text
        channel_name = payload.broadcaster.name
        username     = payload.chatter.name.lower()

        if not content:
            return

        elevated = (
            payload.chatter.moderator
            or payload.chatter.vip
            or payload.chatter.broadcaster
        )
        if elevated:
            self._elevated_channels.add(channel_name)
        else:
            self._elevated_channels.discard(channel_name)

        is_op = self.cfg.is_operator(
            username,
            channel        = channel_name,
            is_mod         = payload.chatter.moderator,
            is_broadcaster = payload.chatter.broadcaster,
        )

        # ── Ambient scripts ────────────────────────────────────────────────
        for script_name in self.cfg.ambient_commands_for_channel(channel_name):
            if not self.gist_manager.script_exists(script_name):
                continue
            if not self._is_script_active(channel_name, script_name):
                continue
            ctx = self._build_job_ctx(
                script_name  = script_name,
                channel_name = channel_name,
                username     = username,
                args         = content.split(),
                msg_id       = payload.id,
                is_ambient   = True,
                is_op        = is_op,
                payload      = payload,
            )
            asyncio.create_task(
                self._run_script(channel_name, ctx, payload),
                name=f"ambient:{script_name}:{channel_name}",
            )

        # ── Command parsing ────────────────────────────────────────────────
        if not content.startswith(self.cfg.bot.prefix):
            return

        parts = content[len(self.cfg.bot.prefix):].split()
        if not parts:
            return

        cmd  = parts[0].lower().replace("\u034f", "").strip()
        args = [
            a.replace("\u034f", "").strip()
            for a in parts[1:]
            if a.replace("\u034f", "").strip()
        ]
        if not cmd:
            return

        logger.debug("[#%s] <%s> !%s %s", channel_name, username, cmd, args)

        # Built-ins
        if cmd == "refresh":
            await self._handle_refresh(payload, channel_name, username, is_op, args)
            return
        if cmd in ("enable", "disable"):
            await self._handle_enable_disable(
                payload, channel_name, username, is_op,
                enable=(cmd == "enable"), args=args,
            )
            return
        if cmd == "groups":
            await self._handle_groups(payload, channel_name, username, is_op)
            return

        # Community scripts
        if cmd not in self.cfg.commands_for_channel(channel_name):
            return
        if not self.gist_manager.script_exists(cmd):
            logger.warning("!%s in #%s: not yet downloaded", cmd, channel_name)
            return
        if not self._is_script_active(channel_name, cmd):
            return

        asyncio.create_task(
            self._auto_refresh(payload, cmd),
            name=f"auto-refresh:{cmd}",
        )

        ctx = self._build_job_ctx(
            script_name  = cmd,
            channel_name = channel_name,
            username     = username,
            args         = args,
            msg_id       = payload.id,
            is_ambient   = False,
            is_op        = is_op,
            payload      = payload,
        )
        logger.info(
            "[#%s|%s] <%s>%s !%s %s",
            channel_name,
            "elevated" if self.rate_limiter.is_elevated(channel_name) else "regular",
            username, "[op]" if is_op else "",
            cmd, " ".join(args),
        )
        await self._run_script(channel_name, ctx, payload)

    # ── Channel event handlers ─────────────────────────────────────────────

    async def event_stream_online(self, payload: twitchio.StreamOnline) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
        except AttributeError:
            logger.warning("stream.online: cannot determine channel name")
            return
        logger.info("stream.online: #%s", channel_name)
        await self._fire_trigger(
            channel_name, "stream.online",
            event_data={"stream_type": getattr(payload, "stream_type", "live")},
        )

    async def event_stream_offline(self, payload: twitchio.StreamOffline) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
        except AttributeError:
            logger.warning("stream.offline: cannot determine channel name")
            return
        logger.info("stream.offline: #%s", channel_name)
        await self._fire_trigger(channel_name, "stream.offline", event_data={})

    async def event_follow(self, payload: twitchio.ChannelFollow) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
            follower     = payload.user
        except AttributeError:
            logger.warning("channel.follow: malformed payload")
            return
        logger.info("channel.follow: %s → #%s", follower.name, channel_name)
        await self._fire_trigger(
            channel_name, "channel.follow",
            event_data={
                "from_user":         follower.name,
                "from_user_display": getattr(follower, "display_name", follower.name),
            },
        )

    async def event_raid(self, payload: twitchio.ChannelRaid) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name   = payload.to_broadcaster.name
            from_user      = payload.from_broadcaster
            viewer_count   = payload.viewer_count
        except AttributeError:
            logger.warning("channel.raid: malformed payload")
            return
        logger.info(
            "channel.raid: %s (%d viewers) → #%s",
            from_user.name, viewer_count, channel_name,
        )
        await self._fire_trigger(
            channel_name, "channel.raid",
            event_data={
                "from_user":         from_user.name,
                "from_user_display": getattr(from_user, "display_name", from_user.name),
                "viewer_count":      viewer_count,
            },
        )

    async def event_ad_break(self, payload: twitchio.ChannelAdBreakBegin) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
            duration     = payload.duration_seconds
            is_automatic = payload.is_automatic
        except AttributeError:
            logger.warning("channel.ad_break: malformed payload")
            return
        logger.info(
            "channel.ad_break: %ds %s #%s",
            duration, "automatic" if is_automatic else "manual", channel_name,
        )
        await self._fire_trigger(
            channel_name, "channel.ad_break",
            event_data={
                "duration":     duration,
                "is_automatic": is_automatic,
            },
        )

    async def event_error(self, payload: twitchio.EventErrorPayload) -> None:
        logger.error(
            "twitchio error in %s: %s",
            payload.listener, payload.error, exc_info=payload.error,
        )

    # ── Watchdog ──────────────────────────────────────────────────────────

    async def _watchdog(self) -> None:
        timeout = self.cfg.bot.watchdog_timeout
        logger.debug("Watchdog started (timeout=%ds)", timeout)
        while True:
            await asyncio.sleep(_WATCHDOG_POLL)
            elapsed = time.monotonic() - self._last_event_at
            if elapsed > timeout:
                logger.warning(
                    "Watchdog: no events in %.0fs (threshold=%ds) — closing for restart",
                    elapsed, timeout,
                )
                await self.close()
                return

    # ── Context builders ───────────────────────────────────────────────────

    def _build_job_ctx(
        self,
        script_name:  str,
        channel_name: str,
        username:     str,
        args:         list[str],
        msg_id:       str,
        is_ambient:   bool,
        is_op:        bool,
        payload:      twitchio.ChatMessage | None = None,
    ) -> JobContext:
        reply_user = reply_msg = reply_msg_id = ""
        if payload and payload.reply:
            reply_user   = payload.reply.parent_user.name
            reply_msg    = payload.reply.parent_message_body
            reply_msg_id = payload.reply.parent_message_id

        return JobContext(
            script_name      = script_name,
            channel          = channel_name,
            user             = username,
            args             = args,
            msg_id           = msg_id,
            timestamp        = time.time(),
            prefix           = self.cfg.bot.prefix,
            bot_nick         = self.cfg.bot.nick,
            is_ambient       = is_ambient,
            is_operator      = is_op,
            channel_dir      = self.cfg.bot.working_dir / channel_name,
            global_dir       = self.cfg.bot.working_dir,
            reply_user       = reply_user,
            reply_message    = reply_msg,
            reply_message_id = reply_msg_id,
        )

    def _build_trigger_ctx(
        self,
        script_name:  str,
        channel_name: str,
        event_type:   str,
        event_data:   dict,
    ) -> JobContext:
        return JobContext(
            script_name = script_name,
            channel     = channel_name,
            user        = "",
            args        = [f"event:{event_type}"],
            msg_id      = "",
            timestamp   = time.time(),
            prefix      = self.cfg.bot.prefix,
            bot_nick    = self.cfg.bot.nick,
            is_ambient  = True,
            is_operator = True,
            channel_dir = self.cfg.bot.working_dir / channel_name,
            global_dir  = self.cfg.bot.working_dir,
            event_data  = event_data,
        )

    # ── Script dispatch ────────────────────────────────────────────────────

    async def _dispatch_script(
        self, job_ctx: JobContext
    ) -> AsyncGenerator[ChatLine | Action | str, None]:
        script_path = self.gist_manager.script_path(job_ctx.script_name)
        if is_v2(script_path):
            async for item in self.worker_manager.submit(
                script_name = job_ctx.script_name,
                channel     = job_ctx.channel,
                ctx_blob    = job_ctx.to_dict(),
                is_ambient  = job_ctx.is_ambient,
                username    = job_ctx.user,
            ):
                yield item
        else:
            async for line in await self.runner.run(job_ctx):
                yield line

    async def _run_script(
        self,
        channel_name: str,
        ctx:          JobContext,
        payload:      twitchio.ChatMessage | None,
    ) -> None:
        try:
            async for item in self._dispatch_script(ctx):
                await self._handle_output(channel_name, item, payload)
        except Exception as exc:
            logger.error(
                "Script %r failed in #%s: %s", ctx.script_name, channel_name, exc
            )

    async def _handle_output(
        self,
        channel_name: str,
        item:         ChatLine | Action | str,
        payload:      twitchio.ChatMessage | None,
    ) -> None:
        if isinstance(item, str):
            await self._send_to_channel(channel_name, item)
        elif isinstance(item, ChatLine):
            await self._send_to_channel(channel_name, item.text)
        elif isinstance(item, Action):
            await self._handle_action(channel_name, item, payload)

    async def _handle_action(
        self,
        channel_name: str,
        action:       Action,
        payload:      twitchio.ChatMessage | None,
    ) -> None:
        kind = action.kind
        data = action.data
        text = data.get("text", "")

        if kind == "error":
            logger.warning(
                "[script:%s] %s", channel_name, data.get("msg", "unknown error")
            )
            return

        if kind == "reply":
            msg_id     = data.get("to") or (payload.id if payload else "")
            reply_user = data.get("user") or (
                payload.chatter.name if payload else ""
            )
            if msg_id:
                await self._send_reply(channel_name, text, msg_id, reply_user)
            elif reply_user:
                await self._send_to_channel(channel_name, f"@{reply_user} {text}")
            else:
                await self._send_to_channel(channel_name, text)
            return

        if kind == "announce":
            color = data.get("color")
            if color not in _ANNOUNCE_COLORS:
                color = None
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster:
                try:
                    await broadcaster.send_announcement(
                        moderator=self.bot_id,
                        message=text,
                        color=color, # defaults to None anyway
                    )
                    return
                except (AttributeError, TypeError) as exc:
                    logger.warning(
                        "send_announcement failed for #%s (%s: %s) — "
                        "falling back to plain message",
                        channel_name, type(exc).__name__, exc,
                    )
            await self._send_to_channel(channel_name, text)
            return

        if kind == "me":
            await self._send_to_channel(channel_name, f"/me {text}")
            return

        if kind == "shoutout":
            target      = data.get("target", "")
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster and target:
                try:
                    await broadcaster.send_shoutout(  # type: ignore[attr-defined]
                        to_broadcaster = target,
                        moderator      = self.cfg.bot.bot_id,
                    )
                    logger.info(
                        "Shoutout sent to %r in #%s", target, channel_name
                    )
                except Exception as exc:
                    logger.warning(
                        "send_shoutout to %r failed in #%s: %s",
                        target, channel_name, exc,
                    )
            return

        if kind == "ban":
            target      = data.get("target", "")
            reason      = data.get("reason", "")
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster and target:
                try:
                    await broadcaster.ban_user(  # type: ignore[attr-defined]
                        token_for = self.cfg.bot.bot_id,
                        user      = target,
                        reason    = reason or None,
                    )
                    logger.info("Banned %r in #%s (reason: %r)", target, channel_name, reason)
                except Exception as exc:
                    logger.warning("ban_user %r in #%s failed: %s", target, channel_name, exc)
            return

        if kind == "timeout":
            target      = data.get("target", "")
            duration    = int(data.get("duration", 600))
            reason      = data.get("reason", "")
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster and target:
                try:
                    await broadcaster.ban_user(  # type: ignore[attr-defined]
                        token_for = self.cfg.bot.bot_id,
                        user      = target,
                        duration  = duration,
                        reason    = reason or None,
                    )
                    logger.info(
                        "Timed out %r for %ds in #%s (reason: %r)",
                        target, duration, channel_name, reason,
                    )
                except Exception as exc:
                    logger.warning(
                        "timeout %r in #%s failed: %s", target, channel_name, exc
                    )
            return

        if kind == "unban":
            target      = data.get("target", "")
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster and target:
                try:
                    await broadcaster.unban_user(  # type: ignore[attr-defined]
                        token_for = self.cfg.bot.bot_id,
                        user      = target,
                    )
                    logger.info("Unbanned %r in #%s", target, channel_name)
                except Exception as exc:
                    logger.warning("unban_user %r in #%s failed: %s", target, channel_name, exc)
            return

        # Unknown kinds: send as plain message if there's text
        if text:
            await self._send_to_channel(channel_name, text)

    # ── Trigger dispatch ───────────────────────────────────────────────────

    async def _fire_trigger(
        self,
        channel_name: str,
        event_type:   str,
        event_data:   dict,
    ) -> None:
        for script_name in self.cfg.triggers.get(event_type, []):
            if not self.gist_manager.script_exists(script_name):
                logger.warning(
                    "Trigger %r: script %r not downloaded", event_type, script_name
                )
                continue
            ctx = self._build_trigger_ctx(
                script_name, channel_name, event_type, event_data
            )
            asyncio.create_task(
                self._run_script(channel_name, ctx, payload=None),
                name=f"trigger:{event_type}:{script_name}:{channel_name}",
            )

    # ── Group state ────────────────────────────────────────────────────────

    def _channel_db_path(self, channel: str) -> Path:
        return self.cfg.bot.working_dir / channel / _CHANNEL_DB

    def _group_enabled(self, channel: str, group_name: str) -> bool:
        path = self._channel_db_path(channel)
        if not path.exists():
            return True
        try:
            with sqlite3.connect(str(path), timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT value FROM kv WHERE namespace='shared' AND key=?",
                    (f"groups:{group_name}:enabled",),
                ).fetchone()
            return json.loads(row[0]) if row else True
        except Exception:
            return True

    def _set_group_enabled(self, channel: str, group_name: str, enabled: bool) -> None:
        path = self._channel_db_path(channel)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(str(path), timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(_KV_DDL)
                conn.execute(
                    """
                    INSERT INTO kv (namespace, key, value, updated_at)
                    VALUES ('shared', ?, ?, ?)
                    ON CONFLICT(namespace, key)
                    DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (f"groups:{group_name}:enabled", json.dumps(enabled), int(time.time())),
                )
                conn.commit()
        except Exception as exc:
            logger.error("Failed to write group state for #%s: %s", channel, exc)

    def _is_script_active(self, channel: str, script_name: str) -> bool:
        for group_name in self.cfg.script_groups.get(script_name, set()):
            if not self._group_enabled(channel, group_name):
                return False
        return True

    # ── Built-in command handlers ──────────────────────────────────────────

    async def _handle_refresh(
        self, payload, channel_name, username, is_op, args,
    ) -> None:
        if not is_op:
            await self._send(payload, f"@{username} !refresh is operator-only")
            return

        limit  = self.cfg.bot.refresh_user_limit
        window = self.cfg.bot.refresh_user_window
        now    = time.monotonic()
        ts     = self._refresh_timestamps.setdefault(username, deque())
        while ts and now - ts[0] >= window:
            ts.popleft()
        if len(ts) >= limit:
            wait = int(window - (now - ts[0])) + 1
            await self._send(
                payload,
                f"@{username} slow down — !refresh limited to {limit} per "
                f"{int(window)}s (retry in ~{wait}s)",
            )
            return

        ts.append(now)

        if args:
            target = args[0].lower()
            if target not in self.gist_manager.scripts:
                await self._send(payload, f"@{username} unknown script: {target}")
                return
            self.gist_manager._gist_updated_at.pop(target, None)
            self.gist_manager._github_shas.pop(target, None)
            updated = await self.gist_manager.fetch_one(
                target, self.gist_manager.scripts[target]
            )
            await self._send(
                payload,
                f"@{username} {'updated' if updated else 'already up to date'}: {target}",
            )
        else:
            self.gist_manager._gist_updated_at.clear()
            self.gist_manager._github_shas.clear()
            results = await self.gist_manager.fetch_all()
            changed = [n for n, ok in results.items() if ok]
            await self._send(
                payload,
                f"@{username} updated: {', '.join(sorted(changed))}"
                if changed else f"@{username} all scripts already up to date",
            )

    async def _handle_enable_disable(
        self, payload, channel_name, username, is_op, enable, args,
    ) -> None:
        verb = "enable" if enable else "disable"
        if not is_op:
            await self._send(payload, f"@{username} !{verb} is operator-only")
            return

        channel_groups = self.cfg.groups_for_channel(channel_name)
        if not args:
            await self._send(
                payload,
                f"Usage: !{verb} <group> | !{verb} all — "
                f"groups: {', '.join(sorted(channel_groups)) or 'none defined'}",
            )
            return

        target = args[0].lower()
        if target == "all":
            for g in channel_groups:
                self._set_group_enabled(channel_name, g, enable)
            await self._send(
                payload,
                f"@{username} {'enabled' if enable else 'disabled'} all groups "
                f"({', '.join(sorted(channel_groups)) or 'none'})",
            )
            return

        if target not in self.cfg.groups:
            await self._send(
                payload,
                f"@{username} unknown group {target!r}. "
                f"Groups: {', '.join(sorted(self.cfg.groups)) or 'none defined'}",
            )
            return

        self._set_group_enabled(channel_name, target, enable)
        await self._send(
            payload,
            f"@{username} {'enabled' if enable else 'disabled'} group '{target}'",
        )

    async def _handle_groups(self, payload, channel_name, username, is_op) -> None:
        if not is_op:
            await self._send(payload, f"@{username} !groups is operator-only")
            return
        channel_groups = self.cfg.groups_for_channel(channel_name)
        if not channel_groups:
            await self._send(payload, f"@{username} no groups defined")
            return
        parts = [
            f"{n}:{'on' if self._group_enabled(channel_name, n) else 'off'}"
            for n in sorted(channel_groups)
        ]
        await self._send(payload, f"@{username} groups — {' | '.join(parts)}")

    async def _auto_refresh(self, payload, script_name: str) -> None:
        bot_key = f"\x00bot:{self.cfg.bot.nick}"
        limit   = self.cfg.bot.refresh_user_limit
        window  = self.cfg.bot.refresh_user_window
        now     = time.monotonic()
        ts      = self._refresh_timestamps.setdefault(bot_key, deque())
        while ts and now - ts[0] >= window:
            ts.popleft()
        if len(ts) >= limit:
            return
        ts.append(now)

        url = self.gist_manager.scripts.get(script_name)
        if not url:
            return
        self.gist_manager._gist_updated_at.pop(script_name, None)
        self.gist_manager._github_shas.pop(script_name, None)
        try:
            updated = await self.gist_manager.fetch_one(script_name, url)
        except Exception as exc:
            logger.error("Auto-refresh of %r failed: %s", script_name, exc)
            return
        if updated:
            await self._send(
                payload,
                f"⟳ {script_name} updated — "
                f"run {self.cfg.bot.prefix}{script_name} again for the latest version",
            )

    # ── Send primitives ────────────────────────────────────────────────────

    async def _send_to_channel(self, channel_name: str, text: str) -> None:
        broadcaster = self._broadcasters.get(channel_name)
        if not broadcaster:
            logger.warning(
                "_send_to_channel: no broadcaster cached for #%s", channel_name
            )
            return
        text = _deduplicate(text, self._last_sent.get(channel_name, ""))
        self._last_sent[channel_name] = text
        await self.rate_limiter.wait_and_send(
            channel_name,
            broadcaster.send_message(
                text, sender=self.bot_id, token_for=self.bot_id
            ),
        )

    async def _send(self, payload: twitchio.ChatMessage, text: str) -> None:
        await self._send_to_channel(payload.broadcaster.name, text)

    async def _send_reply(
        self,
        channel_name:    str,
        text:            str,
        reply_to_msg_id: str,
        reply_to_user:   str = "",
    ) -> None:
        broadcaster = self._broadcasters.get(channel_name)
        if not broadcaster:
            await self._send_to_channel(channel_name, text)
            return

        text = _deduplicate(text, self._last_sent.get(channel_name, ""))
        self._last_sent[channel_name] = text
        try:
            await self.rate_limiter.wait_and_send(
                channel_name,
                broadcaster.send_message(
                    text,
                    sender=self.bot_id,
                    token_for=self.bot_id,
                    reply_parent_message_id=reply_to_msg_id,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Reply to msg_id=%s in #%s failed (%s: %s) — falling back to @mention",
                reply_to_msg_id, channel_name, type(exc).__name__, exc,
            )
            fallback = f"@{reply_to_user} {text}" if reply_to_user else text
            await self.rate_limiter.wait_and_send(
                channel_name,
                broadcaster.send_message(
                    fallback, sender=self.bot_id, token_for=self.bot_id
                ),
            )
