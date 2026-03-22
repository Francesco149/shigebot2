"""
Configuration loading for Shigebot.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class BotConfig:
    nick: str        # bot's Twitch login name (display/logging only)
    bot_id: str      # bot's numeric Twitch user ID
    prefix: str = "!"
    working_dir: Path = Path("/var/lib/shigebot/scripts")
    gist_refresh_interval: int = 300
    script_timeout: int = 10
    script_preamble: str = ""

    # Per-user rate limit for the !refresh command
    refresh_user_limit: int = 10
    refresh_user_window: float = 60.0

    # Twitch rate limiting — one shared counter, two thresholds.
    # See ratelimit.py for details.
    rate_limit_window: float = 30.0
    rate_limit_non_elevated_max: int = 18  # cap for regular channels
    rate_limit_elevated_max: int = 95      # cap for mod/VIP/broadcaster channels


@dataclass
class Config:
    bot: BotConfig
    # channel_name -> resolved sets of (normal_commands, ambient_commands)
    channels: dict[str, tuple[set[str], set[str]]] = field(default_factory=dict)
    # script_name -> gist URL
    scripts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)

        raw_bot = data.get("bot", {})
        bot = BotConfig(
            nick=raw_bot["nick"],
            bot_id=raw_bot["bot_id"],
            prefix=raw_bot.get("prefix", "!"),
            working_dir=Path(raw_bot.get("working_dir", "/var/lib/shigebot/scripts")),
            gist_refresh_interval=raw_bot.get("gist_refresh_interval", 300),
            script_timeout=raw_bot.get("script_timeout", 10),
            script_preamble=raw_bot.get("script_preamble", ""),
            refresh_user_limit=raw_bot.get("refresh_user_limit", 10),
            refresh_user_window=float(raw_bot.get("refresh_user_window", 60.0)),
            rate_limit_window=float(raw_bot.get("rate_limit_window", 30.0)),
            rate_limit_non_elevated_max=raw_bot.get("rate_limit_non_elevated_max", 18),
            rate_limit_elevated_max=raw_bot.get("rate_limit_elevated_max", 95),
        )

        raw_scripts: dict[str, str] = {}
        for name, url in data.get("scripts", {}).items():
            if name.startswith(("-", "@")):
                raise ValueError(
                    f"Script name {name!r} starts with '-' or '@', "
                    "which are reserved for channel command list syntax."
                )
            if not isinstance(url, str):
                raise ValueError(f"scripts.{name} must be a gist URL string")
            raw_scripts[name] = url

        all_script_names = set(raw_scripts)

        channels: dict[str, set[str]] = {}
        for ch, entries in data.get("channels", {}).items():
            if not isinstance(entries, list):
                raise ValueError(f"channels.{ch} must be a list")
            channels[ch] = _resolve_commands(ch, entries, all_script_names)

        return cls(bot=bot, channels=channels, scripts=raw_scripts)

    def get_client_id(self) -> str:
        return self._require_env("TWITCH_CLIENT_ID")

    def get_client_secret(self) -> str:
        return self._require_env("TWITCH_CLIENT_SECRET")

    def get_bot_token_pair(self) -> tuple[str, str]:
        token = self._require_env("TWITCH_BOT_TOKEN")
        refresh = self._require_env("TWITCH_BOT_REFRESH")
        return token, refresh

    def _require_env(self, var: str) -> str:
        val = os.environ.get(var, "").strip()
        if not val:
            raise RuntimeError(
                f"Environment variable {var!r} is not set. "
                "See the README for setup instructions."
            )
        return val

    def all_channels(self) -> list[str]:
        return list(self.channels.keys())

    def commands_for_channel(self, channel: str) -> set[str]:
        return self.channels.get(channel, (set(), set()))[0]

    def ambient_commands_for_channel(self, channel: str) -> set[str]:
        return self.channels.get(channel, (set(), set()))[1]


def _resolve_commands(
    channel: str,
    entries: list[str],
    all_scripts: set[str],
) -> tuple[set[str], set[str]]:
    """
    Resolve a channel's command list, supporting @all and -exclusions.

    Examples:
        ["8ball", "flip"]           -- explicit list
        ["@all"]                    -- every script
        ["@all", "-8ball"]          -- every script except 8ball
        ["@all", "#twitter"]        -- every message triggers twitter
    """
    result: set[str] = set()
    ambient: set[str] = set()
    exclusions: set[str] = set()

    for entry in entries:
        if entry == "@all":
            result |= all_scripts

        elif entry.startswith("#"):
            name = entry[1:]
            if not name:
                raise ValueError(f"channels.{channel}: bare '#' is not valid")
            if name not in all_scripts:
                raise ValueError(
                    f"channels.{channel}: ambient command {name!r} not defined"
                )
            ambient.add(name)

        elif entry.startswith("-"):
            name = entry[1:]
            if not name:
                raise ValueError(f"channels.{channel}: bare '-' is not valid")
            exclusions.add(name)

        else:
            if entry not in all_scripts:
                raise ValueError(
                    f"channels.{channel}: command {entry!r} is not defined"
                )
            result.add(entry)

    result -= exclusions
    result -= ambient
    ambient -= exclusions

    return result, ambient