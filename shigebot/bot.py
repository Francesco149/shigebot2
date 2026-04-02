"""
shigebot/bot.py — twitchio v3 bot. (SPEC 2.1)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import AsyncGenerator

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

# KV DDL — must stay in sync with shigebot.py (SPEC §5)
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

        self._elevated_channels:  set[str]                    = set()
        self._last_sent:          dict[str, str]              = {}
        self._refresh_timestamps: dict[str, deque[float]]     = {}
        self._broadcasters:       dict[str, twitchio.PartialUser] = {}
        self._last_event_at:      float                       = time.monotonic()

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
        subscribe_stream_events = bool(self.cfg.triggers)

        for channel_name in self.cfg.all_channels():
            users = await self.fetch_users(logins=[channel_name])
            if not users:
                logger.error("Cannot resolve channel %r — skipping.", channel_name)
                continue

            broadcaster = users[0]
            self._broadcasters[channel_name] = broadcaster

            await self.subscribe_websocket(
                eventsub.ChatMessageSubscription(
                    broadcaster_user_id = broadcaster.id,
                    user_id             = self.cfg.bot.bot_id,
                ),
                as_bot=True,
            )
            logger.info("Subscribed to chat in #%s (id=%s)", channel_name, broadcaster.id)

            if subscribe_stream_events:
                if "stream.online" in self.cfg.triggers:
                    await self.subscribe_websocket(
                        eventsub.StreamOnlineSubscription(broadcaster_user_id=broadcaster.id),
                        as_bot=True,
                    )
                if "stream.offline" in self.cfg.triggers:
                    await self.subscribe_websocket(
                        eventsub.StreamOfflineSubscription(broadcaster_user_id=broadcaster.id),
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

        # Update elevation cache
        elevated = payload.chatter.moderator or payload.chatter.vip or payload.chatter.broadcaster
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
        args = [a.replace("\u034f", "").strip() for a in parts[1:]
                if a.replace("\u034f", "").strip()]
        if not cmd:
            return

        logger.debug("[#%s] <%s> !%s %s", channel_name, username, cmd, args)

        # ── Built-ins (operator-only) ─────────────────────────────────────
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

        # ── Community scripts ──────────────────────────────────────────────
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

    # ── Stream events ──────────────────────────────────────────────────────

    async def event_stream_online(self, payload: twitchio.StreamOnline) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
        except AttributeError:
            logger.warning("stream.online: cannot determine channel name")
            return
        logger.info("stream.online: #%s", channel_name)
        await self._fire_trigger(channel_name, "stream.online",
                                 [f"stream_type:{getattr(payload, 'stream_type', 'live')}"])

    async def event_stream_offline(self, payload: twitchio.StreamOffline) -> None:  # type: ignore[name-defined]
        self._last_event_at = time.monotonic()
        try:
            channel_name = payload.broadcaster.name
        except AttributeError:
            logger.warning("stream.offline: cannot determine channel name")
            return
        logger.info("stream.offline: #%s", channel_name)
        await self._fire_trigger(channel_name, "stream.offline", [])

    async def event_error(self, payload: twitchio.EventErrorPayload) -> None:
        logger.error("twitchio error in %s: %s", payload.listener, payload.error,
                     exc_info=payload.error)

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
        extra_args:   list[str],
    ) -> JobContext:
        return JobContext(
            script_name      = script_name,
            channel          = channel_name,
            user             = "",
            args             = [f"event:{event_type}"] + extra_args,
            msg_id           = "",
            timestamp        = time.time(),
            prefix           = self.cfg.bot.prefix,
            bot_nick         = self.cfg.bot.nick,
            is_ambient       = True,
            is_operator      = True,   # system events have full operator access
            channel_dir      = self.cfg.bot.working_dir / channel_name,
            global_dir       = self.cfg.bot.working_dir,
        )

    # ── Script dispatch ────────────────────────────────────────────────────

    async def _dispatch_script(
        self, job_ctx: JobContext
    ) -> AsyncGenerator[ChatLine | Action | str, None]:
        """Route a job to the v2 worker pool or v1 subprocess runner."""
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
            logger.error("Script %r failed in #%s: %s", ctx.script_name, channel_name, exc)

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
            logger.warning("[script:%s] %s", channel_name, data.get("msg", "unknown error"))
            return

        if kind == "reply" and payload:
            await self._send_reply(channel_name, text, data.get("to") or payload.id)
            return

        if kind == "announce":
            broadcaster = self._broadcasters.get(channel_name)
            if broadcaster:
                try:
                    await broadcaster.send_announcement(text, token_for=self.bot_id)  # type: ignore[attr-defined]
                    return
                except (AttributeError, TypeError):
                    pass
            await self._send_to_channel(channel_name, text)
            return

        if kind == "me":
            await self._send_to_channel(channel_name, f"/me {text}")
            return

        if text:
            await self._send_to_channel(channel_name, text)

    # ── Trigger dispatch ───────────────────────────────────────────────────

    async def _fire_trigger(
        self,
        channel_name: str,
        event_type:   str,
        extra_args:   list[str],
    ) -> None:
        for script_name in self.cfg.triggers.get(event_type, []):
            if not self.gist_manager.script_exists(script_name):
                logger.warning("Trigger %r: script %r not downloaded", event_type, script_name)
                continue
            ctx = self._build_trigger_ctx(script_name, channel_name, event_type, extra_args)
            asyncio.create_task(
                self._run_script(channel_name, ctx, payload=None),
                name=f"trigger:{event_type}:{script_name}:{channel_name}",
            )

    # ── Group state (direct SQLite into channel.db) ────────────────────────

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
        """Return False if any group containing this script is disabled for the channel."""
        for group_name in self.cfg.script_groups.get(script_name, set()):
            if not self._group_enabled(channel, group_name):
                return False
        return True

    # ── Built-in command handlers ──────────────────────────────────────────

    async def _handle_refresh(
        self,
        payload:      twitchio.ChatMessage,
        channel_name: str,
        username:     str,
        is_op:        bool,
        args:         list[str],
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
                f"@{username} slow down — !refresh is limited to {limit} per "
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
            updated = await self.gist_manager.fetch_one(target, self.gist_manager.scripts[target])
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
        self,
        payload:      twitchio.ChatMessage,
        channel_name: str,
        username:     str,
        is_op:        bool,
        enable:       bool,
        args:         list[str],
    ) -> None:
        verb = "enable" if enable else "disable"

        if not is_op:
            await self._send(payload, f"@{username} !{verb} is operator-only")
            return

        if not args:
            channel_groups = self.cfg.groups_for_channel(channel_name)
            await self._send(
                payload,
                f"Usage: !{verb} <group> | !{verb} all — "
                f"groups: {', '.join(sorted(channel_groups)) or 'none defined'}",
            )
            return

        target         = args[0].lower()
        channel_groups = self.cfg.groups_for_channel(channel_name)

        if target == "all":
            for group_name in channel_groups:
                self._set_group_enabled(channel_name, group_name, enable)
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

    async def _handle_groups(
        self,
        payload:      twitchio.ChatMessage,
        channel_name: str,
        username:     str,
        is_op:        bool,
    ) -> None:
        if not is_op:
            await self._send(payload, f"@{username} !groups is operator-only")
            return

        channel_groups = self.cfg.groups_for_channel(channel_name)
        if not channel_groups:
            await self._send(payload, f"@{username} no groups defined")
            return

        parts = [
            f"{name}:{'on' if self._group_enabled(channel_name, name) else 'off'}"
            for name in sorted(channel_groups)
        ]
        await self._send(payload, f"@{username} groups — {' | '.join(parts)}")

    async def _auto_refresh(self, payload: twitchio.ChatMessage, script_name: str) -> None:
        """Background freshness check on every command invocation."""
        bot_key = f"\x00bot:{self.cfg.bot.nick}"
        limit   = self.cfg.bot.refresh_user_limit
        window  = self.cfg.bot.refresh_user_window
        now     = time.monotonic()

        ts = self._refresh_timestamps.setdefault(bot_key, deque())
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
            logger.warning("_send_to_channel: no broadcaster cached for #%s", channel_name)
            return
        text = _deduplicate(text, self._last_sent.get(channel_name, ""))
        self._last_sent[channel_name] = text
        await self.rate_limiter.wait_and_send(
            channel_name,
            broadcaster.send_message(text, sender=self.bot_id, token_for=self.bot_id),
        )

    async def _send(self, payload: twitchio.ChatMessage, text: str) -> None:
        await self._send_to_channel(payload.broadcaster.name, text)

    async def _send_reply(
        self, channel_name: str, text: str, reply_to_msg_id: str
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
        except TypeError:
            await self.rate_limiter.wait_and_send(
                channel_name,
                broadcaster.send_message(text, sender=self.bot_id, token_for=self.bot_id),
            )
