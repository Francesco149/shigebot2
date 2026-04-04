"""
shigebot/__main__.py — entry point with reconnect retry loop.

Usage:
    shigebot [--debug] [config.toml]
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from .bot import Shigebot
from .config import Config
from .gist import GistManager
from .worker_manager import WorkerManager

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF = 2.0
_MAX_BACKOFF     = 60.0


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level  = level,
        format = "%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt= "%Y-%m-%dT%H:%M:%S",
        stream = sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("twitchio").setLevel(logging.INFO)


async def _run_once(config: Config) -> None:
    config.bot.working_dir.mkdir(parents=True, exist_ok=True)

    async with GistManager(
        scripts          = config.scripts,
        working_dir      = config.bot.working_dir,
        refresh_interval = config.bot.gist_refresh_interval,
    ) as gist_manager:

        logger.info("Fetching %d scripts…", len(config.scripts))
        await gist_manager.fetch_all()

        missing = [n for n in config.scripts if not gist_manager.script_exists(n)]
        if missing:
            logger.warning("Scripts not yet downloaded (will retry): %s", missing)

        worker_manager = WorkerManager(config, gist_manager)
        await worker_manager.start()

        bot = Shigebot(config, gist_manager, worker_manager)

        refresh_task = asyncio.create_task(
            gist_manager.refresh_loop(), name="gist-refresh"
        )

        # HTTP API (optional)
        http_task: asyncio.Task | None = None
        if config.bot.http_api_port > 0:
            from .http_api import serve as http_serve
            http_task = asyncio.create_task(
                http_serve(bot, config.bot.http_api_port),
                name="http-api",
            )

        try:
            async with bot:
                await bot.start()
        finally:
            refresh_task.cancel()
            if http_task is not None:
                http_task.cancel()

            # Await cancellations cleanly
            tasks = [refresh_task]
            if http_task is not None:
                tasks.append(http_task)
            for t in tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            await worker_manager.stop()


async def run(config: Config) -> None:
    backoff = _INITIAL_BACKOFF
    attempt = 0

    while True:
        attempt += 1
        logger.info("Starting bot session (attempt %d)…", attempt)
        try:
            await _run_once(config)
            logger.info("Bot session ended cleanly — reconnecting…")
            backoff = _INITIAL_BACKOFF
        except asyncio.CancelledError:
            logger.info("Shutdown requested")
            raise
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error(
                "Bot session crashed: %s — restarting in %.0fs",
                exc, backoff, exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)


def main() -> None:
    args  = sys.argv[1:]
    debug = "--debug" in args
    args  = [a for a in args if a != "--debug"]

    setup_logging(debug=debug)

    config_path = Path(args[0]) if args else Path("shigebot.toml")
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config = Config.load(config_path)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        logger.critical("Failed to load config %s: %s", config_path, exc)
        sys.exit(1)

    try:
        config.get_client_id()
        config.get_client_secret()
        config.get_bot_token_pair()
    except RuntimeError as exc:
        logger.critical(str(exc))
        sys.exit(1)

    try:
        asyncio.run(run(config))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Interrupted — shut down cleanly")


if __name__ == "__main__":
    main()
