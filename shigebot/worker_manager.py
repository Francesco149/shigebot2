"""
shigebot/worker_manager.py — persistent worker pool manager. (SPEC 2.2)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, NamedTuple

logger = logging.getLogger(__name__)

# ── Protocol ───────────────────────────────────────────────────────────────

_ACTION_BYTE = ord("\x00")


def _is_action(line: bytes) -> bool:
    return bool(line) and line[0] == _ACTION_BYTE


def _parse_action(line: bytes) -> dict:
    return json.loads(line[1:])


# ── Output limits and safety ───────────────────────────────────────────────

MAX_LINES = 10
MAX_CHARS = 350

# Lines starting with '/' are Twitch chat commands (ban, timeout, etc.).
# Scripts must use dedicated sb.* helpers (ban, timeout, …) which go through
# the action protocol. Plain sb.say() output that starts with '/' is dropped.
_CHAT_CMD_PREFIX = "/"


def _sanitize_line(text: str, script_name: str) -> str | None:
    """
    Return the text if safe to send, or None if it should be dropped.
    Logs a warning when a line is dropped so script authors are aware.
    """
    if text.startswith(_CHAT_CMD_PREFIX):
        logger.warning(
            "[%s] Dropped unsafe output starting with '/': %r — "
            "use sb.ban() / sb.timeout() / sb.me() etc. instead",
            script_name, text[:80],
        )
        return None
    return text


# ── Busy reply cooldown ────────────────────────────────────────────────────

_BUSY_COOLDOWN = 10.0   # seconds between "bot is busy" replies per user


# ── Script version detection ───────────────────────────────────────────────

def is_v2(script_path: str | Path) -> bool:
    """Return True if the script's first line is '# shigebot: v2'."""
    try:
        with open(script_path, "rb") as f:
            first = f.readline().decode("utf-8", errors="replace").strip()
        return first == "# shigebot: v2"
    except OSError:
        return False


# ── Result types ───────────────────────────────────────────────────────────

class ChatLine(NamedTuple):
    text: str


class Action(NamedTuple):
    kind: str
    data: dict


# ── Per-job container ──────────────────────────────────────────────────────

@dataclass
class _Job:
    job_id:      str
    ctx_blob:    dict
    is_ambient:  bool
    script_name: str                  # for sanitize_line logging
    result_q:    asyncio.Queue        = field(default_factory=lambda: asyncio.Queue())
    drain_event: asyncio.Event        = field(default_factory=asyncio.Event)


# ── Worker process wrapper ─────────────────────────────────────────────────

class _WorkerProcess:
    """Wraps one persistent worker subprocess."""

    def __init__(
        self,
        script_path:     str,
        working_dir:     Path,
        max_invocations: int,
        idle_timeout:    float,
        preamble:        str,
    ) -> None:
        self._script_path     = script_path
        self._working_dir     = working_dir
        self._max_invocations = max_invocations
        self._idle_timeout    = idle_timeout
        self._preamble        = preamble
        self._proc: asyncio.subprocess.Process | None = None
        self.alive = False

    async def start(self) -> None:
        worker_script = Path(__file__).parent / "worker_process.py"
        cmd = [
            sys.executable, "-u",
            str(worker_script),
            self._script_path,
            str(self._max_invocations),
            str(self._idle_timeout),
        ]

        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        wd = str(self._working_dir)
        parts = [wd] + [p for p in existing.split(os.pathsep) if p and p != wd]
        env["PYTHONPATH"] = os.pathsep.join(parts)

        if self._preamble:
            env["SHIGEBOT_PREAMBLE"] = self._preamble
        else:
            env.pop("SHIGEBOT_PREAMBLE", None)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin  = asyncio.subprocess.PIPE,
            stdout = asyncio.subprocess.PIPE,
            stderr = asyncio.subprocess.PIPE,
            cwd    = str(self._working_dir),
            env    = env,
        )
        self.alive = True

        stem = Path(self._script_path).stem
        asyncio.create_task(self._drain_stderr(), name=f"stderr:{stem}")
        # ── Idle exit detection ────────────────────────────────────────────
        # Set alive=False as soon as the process exits so _dispatch sees the
        # flag the moment it gets the next job, rather than discovering the
        # dead pipe inside run_job (which would drop that first job).
        asyncio.create_task(self._monitor(),     name=f"monitor:{stem}")

        logger.debug("Worker started: pid=%d script=%s", self._proc.pid, self._script_path)

    async def _monitor(self) -> None:
        """Wait for the process to exit and mark alive=False immediately."""
        assert self._proc
        await self._proc.wait()
        self.alive = False
        logger.debug(
            "Worker exited (code=%s): %s",
            self._proc.returncode, self._script_path,
        )

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            stripped = line.decode("utf-8", errors="replace").rstrip()
            if stripped:
                logger.debug("[worker:%s] %s", Path(self._script_path).stem, stripped)

    async def run_job(self, job: _Job) -> None:
        """
        Send job to the worker, forward output to job.result_q, then place
        the None sentinel. The caller must await job.drain_event after this
        returns before starting the next job.
        """
        assert self._proc and self._proc.stdin and self._proc.stdout

        payload = json.dumps({"job_id": job.job_id, "ctx": job.ctx_blob}) + "\n"
        try:
            self._proc.stdin.write(payload.encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.alive = False
            await job.result_q.put(None)
            return

        line_count = 0
        try:
            async for raw in self._proc.stdout:
                line = raw.rstrip(b"\n")

                if _is_action(line):
                    action = _parse_action(line)
                    kind   = action.get("action")

                    if kind == "done":
                        break
                    if kind == "error":
                        await job.result_q.put(Action(kind="error", data=action))
                        continue
                    await job.result_q.put(Action(kind=kind, data=action))

                else:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    # Safety: drop chat commands that could perform mod actions
                    text = _sanitize_line(text, job.script_name)
                    if text is None:
                        continue
                    if line_count >= MAX_LINES:
                        continue
                    if len(text) > MAX_CHARS:
                        text = text[:MAX_CHARS]
                    line_count += 1
                    await job.result_q.put(ChatLine(text=text))

        except Exception as exc:
            logger.error("Worker read error (%s): %s", self._script_path, exc)
            self.alive = False
        finally:
            await job.result_q.put(None)

    async def stop(self) -> None:
        self.alive = False
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


# ── Per-(script, channel) pool ─────────────────────────────────────────────

@dataclass
class ScriptOptions:
    worker_count: int = 1
    queue_size:   int = 3


class _WorkerPool:
    """
    Pool of `worker_count` workers for one (script, channel) pair.
    All workers share a single asyncio job queue.
    """

    def __init__(
        self,
        script_name:     str,
        channel:         str,
        script_path:     str,
        working_dir:     Path,
        opts:            ScriptOptions,
        max_invocations: int,
        idle_timeout:    float,
        preamble:        str,
        global_counter:  asyncio.Semaphore,
    ) -> None:
        self._script_name    = script_name
        self._channel        = channel
        self._script_path    = script_path
        self._working_dir    = working_dir
        self._opts           = opts
        self._max_invocations = max_invocations
        self._idle_timeout   = idle_timeout
        self._preamble       = preamble
        self._global_counter = global_counter

        self._queue:   asyncio.Queue[_Job]    = asyncio.Queue(maxsize=opts.queue_size)
        self._workers: list[_WorkerProcess]   = []
        self._tasks:   list[asyncio.Task]     = []

        # Busy-reply cooldown: track per-user last-replied timestamp so we
        # don't flood the rate limiter with "bot is busy" messages.
        self._busy_last: dict[str, float] = {}

    def _make_worker(self) -> _WorkerProcess:
        return _WorkerProcess(
            script_path     = self._script_path,
            working_dir     = self._working_dir,
            max_invocations = self._max_invocations,
            idle_timeout    = self._idle_timeout,
            preamble        = self._preamble,
        )

    async def start(self) -> None:
        for i in range(self._opts.worker_count):
            await self._spawn_worker(i)

    async def _spawn_worker(self, index: int) -> None:
        if not self._global_counter._value:
            logger.warning(
                "Global worker cap reached — cannot spawn worker %d for %s:%s",
                index, self._script_name, self._channel,
            )
            return

        w = self._make_worker()
        try:
            await w.start()
        except Exception as exc:
            logger.error(
                "Failed to start worker %d for %s:%s — %s",
                index, self._script_name, self._channel, exc,
            )
            return

        self._global_counter._value -= 1
        self._workers.append(w)
        self._tasks.append(asyncio.create_task(
            self._dispatch(w, index),
            name=f"dispatch:{self._script_name}:{self._channel}:{index}",
        ))

    async def _dispatch(self, worker: _WorkerProcess, index: int) -> None:
        """
        Pull jobs from the queue and feed them to the worker one at a time.

        Idle-exit fix: because _monitor() sets worker.alive=False as soon as
        the process exits, the alive check here fires correctly on the very
        first job after an idle timeout — the job is NOT dropped, it runs on
        the freshly spawned replacement worker.

        Drain fix: awaits drain_event before pulling the next job so output
        from consecutive invocations is never interleaved (SPEC §4.4).
        """
        while True:
            job: _Job = await self._queue.get()

            # ── Respawn dead worker (crash or idle exit) ───────────────────
            if not worker.alive:
                logger.warning(
                    "Worker %d not alive for %s:%s — respawning",
                    index, self._script_name, self._channel,
                )
                await worker.stop()
                try:
                    new_w = self._make_worker()
                    await new_w.start()
                    self._workers[self._workers.index(worker)] = new_w
                    worker = new_w
                except Exception as exc:
                    logger.error(
                        "Respawn failed for %s:%s[%d]: %s",
                        self._script_name, self._channel, index, exc,
                    )
                    if not job.is_ambient:
                        await job.result_q.put(
                            ChatLine("⚠ script worker failed to start — try again later")
                        )
                    await job.result_q.put(None)
                    await job.drain_event.wait()
                    continue

            # ── Run job, then wait for full drain ──────────────────────────
            await worker.run_job(job)
            await job.drain_event.wait()

            # ── Recycle if max_invocations reached ─────────────────────────
            if not worker.alive:
                try:
                    new_w = self._make_worker()
                    await new_w.start()
                    self._workers[self._workers.index(worker)] = new_w
                    worker = new_w
                    logger.debug(
                        "Recycled worker %d for %s:%s",
                        index, self._script_name, self._channel,
                    )
                except Exception as exc:
                    logger.error(
                        "Worker recycle failed for %s:%s[%d]: %s",
                        self._script_name, self._channel, index, exc,
                    )

    def _should_send_busy_reply(self, user: str) -> bool:
        """
        Return True if we should tell `user` the bot is busy.

        Enforces a per-user cooldown to prevent the "bot is busy" message
        itself from piling up in the rate-limiter backlog during rapid spam.
        """
        now = time.monotonic()
        last = self._busy_last.get(user, 0.0)
        if now - last >= _BUSY_COOLDOWN:
            self._busy_last[user] = now
            return True
        return False

    async def submit(
        self,
        ctx_blob:        dict,
        is_ambient:      bool,
        busy_reply_user: str | None,
    ) -> AsyncGenerator[ChatLine | Action, None]:
        """
        Submit a job. Yields output items as they arrive from the worker.
        Sets drain_event in a finally block after the None sentinel is consumed.
        """
        job = _Job(
            job_id      = str(uuid.uuid4()),
            ctx_blob    = ctx_blob,
            is_ambient  = is_ambient,
            script_name = self._script_name,
        )

        if self._queue.full():
            logger.debug(
                "Queue full for %s:%s (ambient=%s)",
                self._script_name, self._channel, is_ambient,
            )
            if not is_ambient and busy_reply_user:
                if self._should_send_busy_reply(busy_reply_user):
                    yield ChatLine(
                        f"@{busy_reply_user} bot is busy, try again in a moment"
                    )
            return

        await self._queue.put(job)

        try:
            while True:
                item = await job.result_q.get()
                if item is None:
                    return
                yield item
        finally:
            job.drain_event.set()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for w in self._workers:
            await w.stop()
        self._global_counter._value += len(self._workers)


# ── Top-level manager ──────────────────────────────────────────────────────

class WorkerManager:
    def __init__(self, config: "Config", gist_manager: "GistManager") -> None:
        self._config     = config
        self._gist_mgr   = gist_manager
        self._pools:     dict[tuple[str, str], _WorkerPool] = {}
        self._global_cap = asyncio.Semaphore(config.bot.worker_max_total)

    async def start(self) -> None:
        logger.info(
            "WorkerManager started (global cap: %d)", self._config.bot.worker_max_total
        )

    async def stop(self) -> None:
        for pool in self._pools.values():
            await pool.stop()
        self._pools.clear()
        logger.info("WorkerManager stopped")

    def _get_or_create_pool(self, script_name: str, channel: str) -> _WorkerPool | None:
        key = (script_name, channel)
        if key in self._pools:
            return self._pools[key]

        script_path = self._gist_mgr.script_path(script_name)
        if not script_path.exists():
            return None

        cfg      = self._config.bot
        opts_raw = self._config.script_options.get(script_name, {})
        opts = ScriptOptions(
            worker_count = opts_raw.get("worker_count", cfg.worker_count),
            queue_size   = opts_raw.get("queue_size",   cfg.worker_queue_size),
        )

        pool = _WorkerPool(
            script_name     = script_name,
            channel         = channel,
            script_path     = str(script_path),
            working_dir     = self._gist_mgr.working_dir,
            opts            = opts,
            max_invocations = cfg.worker_max_invocations,
            idle_timeout    = float(cfg.worker_idle_timeout),
            preamble        = cfg.script_preamble,
            global_counter  = self._global_cap,
        )

        asyncio.create_task(pool.start(), name=f"pool-start:{script_name}:{channel}")
        self._pools[key] = pool
        return pool

    async def submit(
        self,
        script_name: str,
        channel:     str,
        ctx_blob:    dict,
        is_ambient:  bool = False,
        username:    str  = "",
    ) -> AsyncGenerator[ChatLine | Action, None]:
        pool = self._get_or_create_pool(script_name, channel)
        if pool is None:
            logger.warning(
                "submit: no pool for %s:%s — script missing from working_dir?",
                script_name, channel,
            )
            return

        async for item in pool.submit(
            ctx_blob        = ctx_blob,
            is_ambient      = is_ambient,
            busy_reply_user = username if not is_ambient else None,
        ):
            yield item
