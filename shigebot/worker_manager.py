"""
shigebot/worker_manager.py — persistent worker pool manager. (SPEC 2.2)

Manages pools of persistent v2 worker processes, one pool per (script, channel)
pair. v1 scripts continue to use runner.py unchanged.

Key guarantee (§4.4): the dispatcher does not start the next job until the
bot has finished consuming and sending all output from the current job. This
prevents interleaved output from consecutive command invocations and stops
stale buffered lines from being sent after newer output has already gone out.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
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


# ── Output limits ──────────────────────────────────────────────────────────

MAX_LINES = 10
MAX_CHARS = 350


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
    kind: str   # "reply" | "announce" | "me" | "error"
    data: dict


# ── Per-job container ──────────────────────────────────────────────────────

@dataclass
class _Job:
    job_id:     str
    ctx_blob:   dict
    is_ambient: bool
    # Items flow: worker stdout → run_job() → result_q → submit() → bot
    result_q:   asyncio.Queue = field(default_factory=lambda: asyncio.Queue())
    # Set by submit() after the None sentinel is consumed, i.e. after the bot
    # has received every item and (via _run_script) sent them all.
    # The dispatcher awaits this before starting the next job, enforcing the
    # output delivery guarantee (SPEC §4.4).
    drain_event: asyncio.Event = field(default_factory=asyncio.Event)


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

        # PYTHONPATH: working_dir must be first so `import shigebot` in the
        # worker finds working_dir/shigebot.py (the v2 runtime) rather than
        # the shigebot bot package installed in site-packages.
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
        asyncio.create_task(
            self._drain_stderr(),
            name=f"stderr:{Path(self._script_path).stem}",
        )
        logger.debug(
            "Worker started: pid=%d script=%s",
            self._proc.pid, self._script_path,
        )

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            stripped = line.decode("utf-8", errors="replace").rstrip()
            if stripped:
                logger.debug("[worker:%s] %s", Path(self._script_path).stem, stripped)

    async def run_job(self, job: _Job) -> None:
        """
        Send job to the worker, forward output to job.result_q, then put the
        None sentinel. Returns as soon as the sentinel is placed — the caller
        must then await job.drain_event before starting the next job.
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
            # Always place the sentinel so submit() can unblock.
            await job.result_q.put(None)

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

        self._queue:   asyncio.Queue[_Job]   = asyncio.Queue(maxsize=opts.queue_size)
        self._workers: list[_WorkerProcess]  = []
        self._tasks:   list[asyncio.Task]    = []

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
        Pull jobs from the shared queue and feed them to the worker, one at a
        time. Does not pull the next job until the current job's output has
        been fully consumed by the bot (drain_event set). This enforces the
        output delivery guarantee (SPEC §4.4) and prevents interleaving.
        """
        while True:
            job: _Job = await self._queue.get()

            # ── Respawn if crashed ─────────────────────────────────────────
            if not worker.alive:
                logger.warning(
                    "Worker %d crashed for %s:%s — respawning",
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
                    # drain_event will be set by submit() when it consumes None
                    await job.drain_event.wait()
                    continue

            # ── Run the job ────────────────────────────────────────────────
            await worker.run_job(job)

            # Wait until the bot has consumed and sent all output from this
            # job before starting the next one (SPEC §4.4).
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

    async def submit(
        self,
        ctx_blob:        dict,
        is_ambient:      bool,
        busy_reply_user: str | None,
    ) -> AsyncGenerator[ChatLine | Action, None]:
        """
        Submit a job. Yields items as they arrive from the worker.

        Signals job.drain_event in a finally block after all items (including
        the None sentinel) are consumed. The dispatcher awaits this before
        starting the next job.
        """
        job = _Job(
            job_id     = str(uuid.uuid4()),
            ctx_blob   = ctx_blob,
            is_ambient = is_ambient,
        )

        if self._queue.full():
            logger.debug(
                "Queue full for %s:%s (ambient=%s)",
                self._script_name, self._channel, is_ambient,
            )
            if not is_ambient and busy_reply_user:
                yield ChatLine(f"@{busy_reply_user} bot is busy, try again in a moment")
            return

        await self._queue.put(job)

        try:
            while True:
                item = await job.result_q.get()
                if item is None:
                    return
                yield item
        finally:
            # Signal the dispatcher that all output has been consumed.
            # This runs whether the caller finished normally, raised, or was
            # cancelled — ensuring the dispatcher is never permanently blocked.
            job.drain_event.set()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for w in self._workers:
            await w.stop()
        self._global_counter._value += len(self._workers)


# ── Top-level manager ──────────────────────────────────────────────────────

class WorkerManager:
    """
    One instance per bot. Owns all worker pools.

    Usage::

        manager = WorkerManager(config, gist_manager)
        await manager.start()
        ...
        async for item in manager.submit(script_name, channel, ctx_blob, ...):
            ...
        ...
        await manager.stop()
    """

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
        """
        Submit a job for `script_name` in `channel`.

        Yields ChatLine and Action items as the worker produces them.
        Drops silently (ambient) or replies with a busy message (command)
        if the pool queue is full.
        """
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
