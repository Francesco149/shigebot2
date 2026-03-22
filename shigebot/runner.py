"""
ScriptRunner: executes community Python scripts as isolated subprocesses.

Protocol
--------
Each script receives:
  sys.argv  = [script_path, *user_args]  (e.g. ['slots.py', '100'])
  NICK      = Twitch login of the user who ran the command
  CHANNEL   = channel name (without #)
  cwd       = working_dir  (so relative pickle files and cross-script
               imports like "import slotshelp" resolve correctly)

An optional extra preamble (from config) is prepended after the argv patch.

Output is streamed line by line as the script produces it, so scripts that
sleep between outputs (e.g. trivia, mirage) have their lines sent to chat
in real time rather than all at once when the process exits.

Limits:
  - MAX_OUTPUT_LINES total lines per invocation  (mirrors: sed -u 10q)
  - MAX_LINE_LENGTH chars per line               (mirrors: fold -w 350)
  - script_timeout total wall-clock seconds before the process is killed;
    the timeout budget is shared across all output lines
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

from .names import name_to_filename

logger = logging.getLogger(__name__)

MAX_OUTPUT_LINES = 10
MAX_LINE_LENGTH = 350

# Minimal fixed preamble — only patches argv[0] so scripts see their own path.
# Any extra code configured via script_preamble in the TOML is appended after.
_ARGV_PATCH = """\
import sys as _sys
_sys.argv[0] = {script_path!r}
del _sys
"""


class ScriptRunner:
    def __init__(
        self,
        working_dir: Path,
        timeout: float = 10.0,
        extra_preamble: str = "",
    ) -> None:
        self.working_dir = working_dir
        self.timeout = timeout
        self.extra_preamble = extra_preamble

    def _build_source(self, script_path: Path) -> bytes:
        """Return the argv patch + optional extra preamble + script source."""
        argv_patch = _ARGV_PATCH.format(script_path=str(script_path))
        parts = [argv_patch]
        if self.extra_preamble:
            parts.append(self.extra_preamble)
        parts.append(script_path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts).encode("utf-8")

    async def run(
        self,
        *,
        script_name: str,
        channel: str,
        username: str,
        reply_to_user: str,
        reply_to_message: str,
        reply_to_message_id: str,
        msg_id: str,
        timestamp: str,
        prefix: str,
        bot_nick: str,
        args: list[str],
    ) -> AsyncGenerator[str, None]:
        """
        Run a script and stream its output lines as an async generator.

        Each line is yielded as soon as the script writes it, so callers can
        send it to chat immediately rather than waiting for the script to exit.
        The timeout budget covers the entire lifetime of the script — if a line
        takes longer than the remaining budget to appear, the process is killed.

        Usage::

            async for line in runner.run(...):
                await send_to_chat(line)
        """
        return self._stream(
            script_name=script_name,
            channel=channel,
            username=username,
            reply_to_user=reply_to_user,
            reply_to_message=reply_to_message,
            reply_to_message_id=reply_to_message_id,
            msg_id=msg_id,
            timestamp=timestamp,
            prefix=prefix,
            bot_nick=bot_nick,
            args=args,
        )

    async def _stream(
        self,
        *,
        script_name: str,
        channel: str,
        username: str,
        reply_to_user: str,
        reply_to_message: str,
        reply_to_message_id: str,
        msg_id: str,
        timestamp: str,
        prefix: str,
        bot_nick: str,
        args: list[str],
    ) -> AsyncGenerator[str, None]:
        script_path = self.working_dir / name_to_filename(script_name)
        if not script_path.exists():
            logger.warning("Script %r not found at %s", script_name, script_path)
            return

        try:
            source = self._build_source(script_path)
        except OSError as exc:
            logger.error("Failed to read script %r: %s", script_name, exc)
            return

        env = os.environ.copy()
        env["NICK"] = username
        env["CHANNEL"] = channel
        env["REPLY_TO_USER"] = reply_to_user
        env["REPLY_TO_MESSAGE"] = reply_to_message
        env["REPLY_TO_MESSAGE_ID"] = reply_to_message_id
        env["MSG_ID"] = msg_id
        env["TIMESTAMP"] = timestamp
        env["PREFIX"] = prefix
        env["BOT_NICK"] = bot_nick
        # Propagate sys.path so Nix-installed packages (numpy etc.) are visible.
        ppath = sys.path + [ str(self.working_dir) ]
        env["PYTHONPATH"] = os.pathsep.join(p for p in ppath if p)
        # Force unbuffered stdout so lines are sent to the pipe immediately
        # rather than held in Python's internal 8KB write buffer until the
        # script exits. Required for scripts that sleep between outputs
        # (e.g. trivia, mirage). -u is the flag; PYTHONUNBUFFERED=1 covers
        # subprocesses spawned by the script itself.
        env["PYTHONUNBUFFERED"] = "1"

        argv = [sys.executable, "-u", "-", *args]

        # script 
        channel_wd = self.working_dir / channel
        channel_wd.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=channel_wd,
                env=env,
            )
        except OSError as exc:
            logger.error("Failed to spawn script %r: %s", script_name, exc)
            return

        # Write source to stdin and close it so the script sees EOF.
        # This is non-blocking: we write then continue reading stdout.
        assert proc.stdin is not None
        try:
            proc.stdin.write(source)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # script may have already exited

        # Deadline for the entire script run
        deadline = asyncio.get_event_loop().time() + self.timeout
        line_count = 0

        assert proc.stdout is not None

        try:
            while line_count < MAX_OUTPUT_LINES:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    logger.warning(
                        "Script %r timed out after %.1fs (killed)", script_name, self.timeout
                    )
                    break

                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Script %r timed out after %.1fs (killed)", script_name, self.timeout
                    )
                    break

                if not raw:  # EOF
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH]

                line_count += 1
                yield line

        finally:
            # Always clean up the process
            try:
                proc.kill()
            except ProcessLookupError:
                pass

            # Drain stderr for logging
            try:
                _, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=2.0
                )
                if stderr_bytes:
                    for err_line in stderr_bytes.decode("utf-8", errors="replace").splitlines():
                        err_line = err_line.strip()
                        if err_line:
                            logger.warning("[script:%s] %s", script_name, err_line)
            except (asyncio.TimeoutError, Exception):
                pass

            if proc.returncode not in (0, None):
                logger.info("Script %r exited with code %d", script_name, proc.returncode)
