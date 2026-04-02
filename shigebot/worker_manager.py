"""
shigebot/worker_manager.py — persistent worker pool manager. (SPEC 2.1)

Manages a pool of persistent v2 worker processes per (script, channel) pair.
v1 scripts continue to use runner.py unchanged; this module handles only v2.

Integration with bot.py:
    Replace the runner.run() call for v2 scripts with manager.submit().
    Detect v2 with _is_v2(script_path).

    manager = WorkerManager(config, gist_manager)
    await manager.start()
    ...
    if _is_v2(path):
        async for item in manager.submit(script_name, channel, ctx_blob):
            await handle(item)
    else:
        async for line in await runner.run(...):
            await send(line)
    ...
    await manager.stop()
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

# ── Protocol sentinel ──────────────────────────────────────────────────────

_ACTION_BYTE = ord("\x00")


def _is_action(line: bytes) -> bool:
    return bool(line) and line[0] == _ACTION_BYTE


def _parse_action(line: bytes) -> dict:
    return json.loads(line[1:])


# ── Output limits (mirroring runner.py) ───────────────────────────────────

MAX_LINES  = 10
MAX_CHARS  = 350


# ── Script version detection ───────────────────────────────────────────────

def is_v2(script_path: str | Path) -> bool:
    """Return True if the script starts with '# shigebot: v2'."""
    try:
        with open(script_path, "rb") as f:
            first = f.readline().decode("utf-8", errors="replace").strip()
        return first == "# shigebot: v2"
    except OSError:
        return False


# ── Result item types ──────────────────────────────────────────────────────

class ChatLine(NamedTuple):
    """A plain chat message to send."""
    text: str


class Action(NamedTuple):
    """A structured action from the script (reply, announce, me, error)."""
    kind: str      # "reply" | "announce" | "me" | "error"
    data: dict     # full parsed action dict


# ── Per-job result container ───────────────────────────────────────────────

@dataclass
class _Job:
    job_id:    str
    ctx_blob:  dict
    is_ambient: bool
    # Output items flow from dispatcher → submitter through this queue.
    # None is the end-of-stream sentinel.
    result_q: asyncio.Queue = field(default_factory=lambda: asyncio.Queue())


# ── Single worker process wrapper ──────────────────────────────────────────

class _WorkerProcess:
    """
    Wraps one persistent worker subprocess. Feeds it jobs over stdin and
    reads output from stdout.
    """

    def __init__(
        self,
        script_path: str,
        working_dir: Path,
        max_invocations: int,
        idle_timeout: float,
    ) -> None:
        self._script_path     = script_path
        self._working_dir     = working_dir
        self._max_invocations = max_invocations
        self._idle_timeout    = idle_timeout
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
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._working_dir),
        )
        self.alive = True
        # Drain stderr in the background (for logging)
        asyncio.create_task(self._drain_stderr(), name=f"stderr:{Path(self._script_path).stem}")
        logger.debug("Worker started: pid=%d script=%s", self._proc.pid, self._script_path)

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            stripped = line.decode("utf-8", errors="replace").rstrip()
            if stripped:
                logger.debug("[worker:%s] %s", Path(self._script_path).stem, stripped)

    async def run_job(self, job: _Job) -> None:
        """
        Send `job` to the worker, forward output to job.result_q,
        and put the None sentinel when done or on crash.
        """
        assert self._proc and self._proc.stdin and self._proc.stdout

        # Send job descriptor
        payload = json.dumps({"job_id": job.job_id, "ctx": job.ctx_blob}) + "\n"
        try:
            self._proc.stdin.write(payload.encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.alive = False
            await job.result_q.put(None)
            return

        # Read output until the done signal
        line_count = 0
        try:
            async for raw in self._proc.stdout:
                line = raw.rstrip(b"\n")

                if _is_action(line):
                    action = _parse_action(line)
                    kind = action.get("action")

                    if kind == "done":
                        break

                    if kind == "error":
                        await job.result_q.put(Action(kind="error", data=action))
                        # done line follows immediately
                        continue

                    # reply / announce / me
                    await job.result_q.put(Action(kind=kind, data=action))

                else:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
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

        # Detect clean exit (worker recycled itself after max_invocations)
        if self._proc.stdout.at_eof():
            self.alive = False
            logger.debug("Worker exited cleanly: %s", self._script_path)

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


# ── Per-(script, channel) worker pool ─────────────────────────────────────

@dataclass
class ScriptOptions:
    worker_count:  int   = 1
    queue_size:    int   = 3
    # Resolved by WorkerManager based on bot config defaults if not set
    # explicitly in [script_options].


class _WorkerPool:
    """
    A pool of `worker_count` persistent workers for one (script, channel).
    All workers share a single asyncio job queue.
    """

    def __init__(
        self,
        script_name:      str,
        channel:          str,
        script_path:      str,
        working_dir:      Path,
        opts:             ScriptOptions,
        max_invocations:  int,
        idle_timeout:     float,
        global_counter:   "asyncio.Semaphore",
    ) -> None:
        self._script_name    = script_name
        self._channel        = channel
        self._script_path    = script_path
        self._working_dir    = working_dir
        self._opts           = opts
        self._max_invocations = max_invocations
        self._idle_timeout   = idle_timeout
        self._global_counter = global_counter

        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=opts.queue_size)
        self._workers: list[_WorkerProcess] = []
        self._tasks:   list[asyncio.Task]   = []

    async def start(self) -> None:
        for i in range(self._opts.worker_count):
            await self._spawn_worker(i)

    async def _spawn_worker(self, index: int) -> None:
        """Spawn one worker and start its dispatch coroutine."""
        # Honour global process cap
        if not self._global_counter._value:
            logger.warning(
                "Global worker cap reached — cannot spawn worker %d for %s:%s",
                index, self._script_name, self._channel,
            )
            return

        w = _WorkerProcess(
            self._script_path,
            self._working_dir,
            self._max_invocations,
            self._idle_timeout,
        )
        try:
            await w.start()
        except Exception as exc:
            logger.error("Failed to start worker %d for %s:%s — %s",
                         index, self._script_name, self._channel, exc)
            return

        self._global_counter._value -= 1  # consume one global slot
        self._workers.append(w)
        task = asyncio.create_task(
            self._dispatch(w, index),
            name=f"dispatch:{self._script_name}:{self._channel}:{index}",
        )
        self._tasks.append(task)

    async def _dispatch(self, worker: _WorkerProcess, index: int) -> None:
        """
        Coroutine for one worker: pull jobs from the shared queue and
        feed them to the worker process one at a time. Respawns the worker
        on crash.
        """
        while True:
            job: _Job = await self._queue.get()

            if not worker.alive:
                logger.warning("Worker %d crashed for %s:%s — respawning",
                               index, self._script_name, self._channel)
                await worker.stop()
                try:
                    new_w = _WorkerProcess(
                        self._script_path,
                        self._working_dir,
                        self._max_invocations,
                        self._idle_timeout,
                    )
                    await new_w.start()
                    # Replace in list
                    idx = self._workers.index(worker)
                    self._workers[idx] = new_w
                    worker = new_w
                except Exception as exc:
                    logger.error("Respawn failed for %s:%s[%d]: %s",
                                 self._script_name, self._channel, index, exc)
                    # Drop job — crash-loop protection
                    if not job.is_ambient:
                        await job.result_q.put(
                            ChatLine("⚠ script worker failed to start — try again later")
                        )
                    await job.result_q.put(None)
                    continue

            await worker.run_job(job)

            # If the worker exited cleanly (max_invocations reached), respawn.
            if not worker.alive:
                try:
                    new_w = _WorkerProcess(
                        self._script_path,
                        self._working_dir,
                        self._max_invocations,
                        self._idle_timeout,
                    )
                    await new_w.start()
                    idx = self._workers.index(worker)
                    self._workers[idx] = new_w
                    worker = new_w
                    logger.debug("Recycled worker %d for %s:%s",
                                 index, self._script_name, self._channel)
                except Exception as exc:
                    logger.error("Worker recycle failed for %s:%s[%d]: %s",
                                 self._script_name, self._channel, index, exc)

    async def submit(
        self,
        ctx_blob: dict,
        is_ambient: bool,
        busy_reply_user: str | None,
    ) -> AsyncGenerator[ChatLine | Action, None]:
        """
        Submit a job to the pool. Returns an async generator of output items.

        If the queue is full:
          - command: yields a busy ChatLine, returns immediately.
          - ambient: returns immediately with no output.
        """
        job = _Job(
            job_id=str(uuid.uuid4()),
            ctx_blob=ctx_blob,
            is_ambient=is_ambient,
        )

        if self._queue.full():
            logger.debug("Queue full for %s:%s (ambient=%s)",
                         self._script_name, self._channel, is_ambient)
            if not is_ambient and busy_reply_user:
                yield ChatLine(
                    f"@{busy_reply_user} bot is busy, try again in a moment"
                )
            return

        await self._queue.put(job)

        # Stream output as the worker produces it
        async def _stream():
            while True:
                item = await job.result_q.get()
                if item is None:
                    return
                yield item

        async for item in _stream():
            yield item

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for w in self._workers:
            await w.stop()
        self._global_counter._value += len(self._workers)


# ── Worker manager (top-level) ─────────────────────────────────────────────

class WorkerManager:
    """
    Top-level manager. One instance lives in the bot alongside GistManager.

    Usage::

        manager = WorkerManager(config, gist_manager)
        await manager.start()
        ...
        async for item in manager.submit(script_name, channel, ctx_blob, is_ambient, user):
            if isinstance(item, ChatLine):
                await send_chat(item.text)
            elif isinstance(item, Action):
                await handle_action(item)
        ...
        await manager.stop()
    """

    def __init__(self, config: "Config", gist_manager: "GistManager") -> None:
        self._config      = config
        self._gist_mgr    = gist_manager
        self._pools:      dict[tuple[str, str], _WorkerPool] = {}
        # Semaphore value = remaining global worker slots
        self._global_cap  = asyncio.Semaphore(config.bot.worker_max_total)

    async def start(self) -> None:
        logger.info("WorkerManager started (global cap: %d)", self._config.bot.worker_max_total)

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

        cfg = self._config.bot
        opts_raw = self._config.script_options.get(script_name, {})
        opts = ScriptOptions(
            worker_count=opts_raw.get("worker_count", cfg.worker_count),
            queue_size=opts_raw.get(
                "queue_size",
                cfg.ambient_queue_size,   # will be overridden per call if needed
            ),
        )

        # Build working_dir for this channel
        channel_wd = self._gist_mgr.working_dir / channel
        channel_wd.mkdir(parents=True, exist_ok=True)

        pool = _WorkerPool(
            script_name=script_name,
            channel=channel,
            script_path=str(script_path),
            working_dir=self._gist_mgr.working_dir,
            opts=opts,
            max_invocations=cfg.worker_max_invocations,
            idle_timeout=float(cfg.worker_idle_timeout),
            global_counter=self._global_cap,
        )

        # Start pool in background (don't await here to keep submit() fast)
        asyncio.create_task(pool.start(), name=f"pool-start:{script_name}:{channel}")
        self._pools[key] = pool
        return pool

    async def submit(
        self,
        script_name:   str,
        channel:       str,
        ctx_blob:      dict,
        is_ambient:    bool   = False,
        username:      str    = "",
    ) -> AsyncGenerator[ChatLine | Action, None]:
        """
        Submit a job for `script_name` in `channel`.

        Yields ChatLine and Action items as the worker produces them.
        For ambient scripts, yields nothing if the queue is full.
        For command scripts, yields a busy ChatLine if the queue is full.
        """
        pool = self._get_or_create_pool(script_name, channel)
        if pool is None:
            logger.warning("submit: no pool for %s:%s (script missing?)", script_name, channel)
            return

        async for item in pool.submit(
            ctx_blob=ctx_blob,
            is_ambient=is_ambient,
            busy_reply_user=username if not is_ambient else None,
        ):
            yield item
