"""
Shigebot entry point.

Usage::

    shigebot [--debug] [config.toml]

Config path defaults to ``shigebot.toml`` in the current directory.

Required environment variables:
    TWITCH_CLIENT_ID      — from dev.twitch.tv app registration
    TWITCH_CLIENT_SECRET  — from dev.twitch.tv app registration
    TWITCH_BOT_TOKEN      — user access token with chat scopes
    TWITCH_BOT_REFRESH    — refresh token (twitchio rotates automatically)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .bot import Shigebot
from .config import Config
from .gist import GistManager

logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("twitchio").setLevel(logging.INFO)


def main() -> None:
    args = sys.argv[1:]
    debug = "--debug" in args
    args = [a for a in args if a != "--debug"]

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

    config.bot.working_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Working directory: %s", config.bot.working_dir)

    # Validate credentials are present before starting
    try:
        config.get_client_id()
        config.get_client_secret()
        config.get_bot_token_pair()
    except RuntimeError as exc:
        logger.critical(str(exc))
        sys.exit(1)

    # GistManager runs as a background task alongside the bot.
    # twitchio v3's run() manages its own event loop, so we wire the
    # gist refresh into setup_hook via the bot instead.
    import asyncio

    async def run() -> None:
        config.bot.working_dir.mkdir(parents=True, exist_ok=True)

        async with GistManager(
            scripts=config.scripts,
            working_dir=config.bot.working_dir,
            refresh_interval=config.bot.gist_refresh_interval,
        ) as gist_manager:
            logger.info("Fetching %d scripts…", len(config.scripts))
            await gist_manager.fetch_all()

            missing = [n for n in config.scripts if not gist_manager.script_exists(n)]
            if missing:
                logger.warning(
                    "Scripts not yet downloaded (will retry): %s", missing
                )

            bot = Shigebot(config, gist_manager)

            refresh_task = asyncio.create_task(
                gist_manager.refresh_loop(), name="gist-refresh"
            )
            try:
                async with bot:
                    await bot.start()
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, shut down cleanly")


if __name__ == "__main__":
    main()
