"""
shigebot/runner.py — patch notes for JobContext integration.

The only change from the previous version is that _stream() now accepts a
single `ctx: JobContext` instead of ~12 individual keyword arguments.
The public `run()` method signature changes the same way.

Replace the existing run() and _stream() with the versions below.
Everything else in runner.py (constants, _ARGV_PATCH, _build_source) is
unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

from .context import JobContext
from .names import name_to_filename

logger = logging.getLogger(__name__)

MAX_OUTPUT_LINES = 10
MAX_LINE_LENGTH  = 350

_ARGV_PATCH = """\
import sys as _sys
_sys.argv[0] = {script_path!r}
del _sys
"""


class ScriptRunner:
    def __init__(
        self,
        working_dir:    Path,
        timeout:        float = 10.0,
        extra_preamble: str   = "",
    ) -> None:
        self.working_dir    = working_dir
        self.timeout        = timeout
        self.extra_preamble = extra_preamble

    def _build_source(self, script_path: Path) -> bytes:
        patch = _ARGV_PATCH.format(script_path=str(script_path))
        parts = [patch]
        if self.extra_preamble:
            parts.append(self.extra_preamble)
        parts.append(script_path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts).encode("utf-8")

    async def run(self, ctx: JobContext) -> AsyncGenerator[str, None]:
        """
        Run a v1 script and stream its stdout lines.
        Takes a single JobContext instead of individual keyword arguments.
        """
        return self._stream(ctx)

    async def _stream(self, ctx: JobContext) -> AsyncGenerator[str, None]:
        script_path = self.working_dir / name_to_filename(ctx.script_name)
        if not script_path.exists():
            logger.warning("Script %r not found at %s", ctx.script_name, script_path)
            return

        try:
            source = self._build_source(script_path)
        except OSError as exc:
            logger.error("Failed to read script %r: %s", ctx.script_name, exc)
            return

        env = os.environ.copy()
        # Legacy v1 env vars
        env.update(ctx.to_v1_env())
        # SHIGEBOT_CTX for v1 scripts that opt in to the v2 context
        env["SHIGEBOT_CTX"] = json.dumps(ctx.to_dict())

        ppath = sys.path + [str(self.working_dir)]
        env["PYTHONPATH"]     = os.pathsep.join(p for p in ppath if p)
        env["PYTHONUNBUFFERED"] = "1"

        argv      = [sys.executable, "-u", "-", *ctx.args]
        channel_wd = self.working_dir / ctx.channel
        channel_wd.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin  = asyncio.subprocess.PIPE,
                stdout = asyncio.subprocess.PIPE,
                stderr = asyncio.subprocess.PIPE,
                cwd    = channel_wd,
                env    = env,
            )
        except OSError as exc:
            logger.error("Failed to spawn script %r: %s", ctx.script_name, exc)
            return

        assert proc.stdin is not None
        try:
            proc.stdin.write(source)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        deadline   = asyncio.get_event_loop().time() + self.timeout
        line_count = 0

        assert proc.stdout is not None
        try:
            while line_count < MAX_OUTPUT_LINES:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    logger.warning("Script %r timed out", ctx.script_name)
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.warning("Script %r timed out", ctx.script_name)
                    break

                if not raw:
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH]
                line_count += 1
                yield line

        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                if stderr_bytes:
                    for err_line in stderr_bytes.decode("utf-8", errors="replace").splitlines():
                        err_line = err_line.strip()
                        if err_line:
                            logger.warning("[script:%s] %s", ctx.script_name, err_line)
            except (asyncio.TimeoutError, Exception):
                pass

            if proc.returncode not in (0, None):
                logger.info("Script %r exited with code %d", ctx.script_name, proc.returncode)
