"""
shigebot/config.py — configuration loading.

Canonical TOML format (see shigebot.toml.example for a full annotated file):

    [bot]
    nick    = "shigebot"
    bot_id  = "123456789"
    prefix  = "!"
    operators = ["painketsu", "@mods", "@streamer"]

    [groups]
    games = ["slots", "rr", "fish", "trivia", "mirage", "bank"]
    fun   = ["8ball", "flip", "slap", "pepe", "4/4"]

    [triggers]
    "stream.online"  = ["announce_live"]
    "stream.offline" = ["announce_offline"]

    [channels]
    mychannel = ["@all", "#lurk", "#logs", "-ratelimit"]

    [scripts]
    hi           = "https://gist.github.com/..."
    announce_live = "github:owner/repo:scripts/announce.py@main"

    [script_options.lurk]
    worker_count = 3
    queue_size   = 20

    [script_options.logs]
    queue_size = 100
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


# ── Operator spec validation ───────────────────────────────────────────────

_OPERATOR_SPECIALS = frozenset({"@mods", "@streamer"})


def _validate_operator(spec: str) -> None:
    if spec.startswith("@") and spec not in _OPERATOR_SPECIALS:
        raise ValueError(
            f"Invalid operator spec {spec!r}. "
            f"Special values: {sorted(_OPERATOR_SPECIALS)}"
        )


# ── BotConfig ─────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    nick:    str           # bot's Twitch login (display/logging only)
    bot_id:  str           # bot's numeric Twitch user ID

    prefix:      str  = "!"
    working_dir: Path = field(default_factory=lambda: Path("/var/lib/shigebot/scripts"))

    # Who can use operator-only commands (!refresh, !enable, !disable).
    # Entries: exact usernames (lowercase), "@mods", "@streamer".
    operators: list[str] = field(default_factory=list)

    # Gist / source management
    gist_refresh_interval: int = 300
    script_timeout:        int = 10
    script_preamble:       str = ""

    # !refresh rate limit (operator-only, rate-limited anyway as DoS protection)
    refresh_user_limit:  int   = 10
    refresh_user_window: float = 60.0

    # Twitch send rate limits
    rate_limit_window:           float = 30.0
    rate_limit_non_elevated_max: int   = 18
    rate_limit_elevated_max:     int   = 95

    # v2 worker pool defaults
    worker_max_invocations: int = 100   # recycle after N jobs; 0 = never
    worker_idle_timeout:    int = 300   # seconds idle → self-exit; 0 = never
    worker_max_total:       int = 200   # hard cap on total live workers
    worker_count:           int = 1     # workers per (script, channel)
    worker_queue_size:      int = 3     # pending jobs cap — command default
    ambient_queue_size:     int = 0     # pending jobs cap — ambient default

    # Watchdog: restart bot if no events received for this many seconds.
    # 0 disables the watchdog.
    watchdog_timeout: int = 300


# ── Top-level Config ───────────────────────────────────────────────────────

@dataclass
class Config:
    bot: BotConfig

    # channel_name → (command_scripts, ambient_scripts)
    channels: dict[str, tuple[set[str], set[str]]] = field(default_factory=dict)

    # script_name → source URL (gist or "github:owner/repo:path[@ref]")
    scripts: dict[str, str] = field(default_factory=dict)

    # group_name → frozenset of script names (static membership)
    groups: dict[str, frozenset[str]] = field(default_factory=dict)

    # Reverse map: script_name → set of group names containing it
    script_groups: dict[str, set[str]] = field(default_factory=dict)

    # event_type → list of script names to fire
    triggers: dict[str, list[str]] = field(default_factory=dict)

    # script_name → {worker_count, queue_size} overrides
    script_options: dict[str, dict[str, int]] = field(default_factory=dict)

    # ── Loaders ───────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)

        raw_bot = data.get("bot", {})

        operators = raw_bot.get("operators", [])
        for op in operators:
            _validate_operator(op)

        bot = BotConfig(
            nick    = raw_bot["nick"],
            bot_id  = raw_bot["bot_id"],
            prefix  = raw_bot.get("prefix", "!"),
            working_dir = Path(raw_bot.get("working_dir", "/var/lib/shigebot/scripts")),
            operators   = [op.lower() if not op.startswith("@") else op
                           for op in operators],
            gist_refresh_interval = raw_bot.get("gist_refresh_interval", 300),
            script_timeout        = raw_bot.get("script_timeout", 10),
            script_preamble       = raw_bot.get("script_preamble", ""),
            refresh_user_limit    = raw_bot.get("refresh_user_limit", 10),
            refresh_user_window   = float(raw_bot.get("refresh_user_window", 60.0)),
            rate_limit_window            = float(raw_bot.get("rate_limit_window", 30.0)),
            rate_limit_non_elevated_max  = raw_bot.get("rate_limit_non_elevated_max", 18),
            rate_limit_elevated_max      = raw_bot.get("rate_limit_elevated_max", 95),
            worker_max_invocations = raw_bot.get("worker_max_invocations", 100),
            worker_idle_timeout    = raw_bot.get("worker_idle_timeout", 300),
            worker_max_total       = raw_bot.get("worker_max_total", 200),
            worker_count           = raw_bot.get("worker_count", 1),
            worker_queue_size      = raw_bot.get("worker_queue_size", 3),
            ambient_queue_size     = raw_bot.get("ambient_queue_size", 0),
            watchdog_timeout       = raw_bot.get("watchdog_timeout", 300),
        )

        # ── Scripts ───────────────────────────────────────────────────────
        raw_scripts: dict[str, str] = {}
        for name, url in data.get("scripts", {}).items():
            if name.startswith(("-", "@", "#")):
                raise ValueError(
                    f"Script name {name!r} starts with a reserved character."
                )
            if not isinstance(url, str):
                raise ValueError(f"scripts.{name} must be a source URL string")
            raw_scripts[name] = url

        all_scripts = set(raw_scripts)

        # ── Groups ────────────────────────────────────────────────────────
        groups: dict[str, frozenset[str]] = {}
        script_groups: dict[str, set[str]] = {}

        for group_name, members in data.get("groups", {}).items():
            if not isinstance(members, list):
                raise ValueError(f"groups.{group_name} must be a list")
            for member in members:
                if member not in all_scripts:
                    raise ValueError(
                        f"groups.{group_name}: {member!r} not defined in [scripts]"
                    )
            groups[group_name] = frozenset(members)
            for member in members:
                script_groups.setdefault(member, set()).add(group_name)

        # ── Triggers ──────────────────────────────────────────────────────
        triggers: dict[str, list[str]] = {}
        _supported_triggers = frozenset({
            "stream.online", "stream.offline",
        })

        for event_type, scripts in data.get("triggers", {}).items():
            if event_type not in _supported_triggers:
                raise ValueError(
                    f"Unsupported trigger event {event_type!r}. "
                    f"Supported: {sorted(_supported_triggers)}"
                )
            if not isinstance(scripts, list):
                raise ValueError(f"triggers.{event_type!r} must be a list")
            for s in scripts:
                if s not in all_scripts:
                    raise ValueError(
                        f"triggers.{event_type!r}: {s!r} not defined in [scripts]"
                    )
            triggers[event_type] = scripts

        # ── Channels ──────────────────────────────────────────────────────
        channels: dict[str, tuple[set[str], set[str]]] = {}
        for ch, entries in data.get("channels", {}).items():
            if not isinstance(entries, list):
                raise ValueError(f"channels.{ch} must be a list")
            channels[ch] = _resolve_commands(ch, entries, all_scripts)

        # ── Script options ────────────────────────────────────────────────
        script_options: dict[str, dict[str, int]] = {}
        _valid_option_keys = frozenset({"worker_count", "queue_size"})

        for name, opts in data.get("script_options", {}).items():
            if not isinstance(opts, dict):
                raise ValueError(f"script_options.{name} must be a table")
            if name not in all_scripts:
                raise ValueError(
                    f"script_options.{name}: no matching entry in [scripts]"
                )
            unknown = set(opts.keys()) - _valid_option_keys
            if unknown:
                raise ValueError(
                    f"script_options.{name}: unknown keys {unknown}. "
                    f"Valid: {sorted(_valid_option_keys)}"
                )
            script_options[name] = {k: int(v) for k, v in opts.items()}

        return cls(
            bot            = bot,
            channels       = channels,
            scripts        = raw_scripts,
            groups         = groups,
            script_groups  = script_groups,
            triggers       = triggers,
            script_options = script_options,
        )

    # ── Credential accessors ───────────────────────────────────────────────

    def get_client_id(self) -> str:
        return self._require_env("TWITCH_CLIENT_ID")

    def get_client_secret(self) -> str:
        return self._require_env("TWITCH_CLIENT_SECRET")

    def get_bot_token_pair(self) -> tuple[str, str]:
        return (
            self._require_env("TWITCH_BOT_TOKEN"),
            self._require_env("TWITCH_BOT_REFRESH"),
        )

    def _require_env(self, var: str) -> str:
        val = os.environ.get(var, "").strip()
        if not val:
            raise RuntimeError(
                f"Environment variable {var!r} is not set. "
                "See the README for setup instructions."
            )
        return val

    # ── Channel queries ────────────────────────────────────────────────────

    def all_channels(self) -> list[str]:
        return list(self.channels.keys())

    def commands_for_channel(self, channel: str) -> set[str]:
        return self.channels.get(channel, (set(), set()))[0]

    def ambient_commands_for_channel(self, channel: str) -> set[str]:
        return self.channels.get(channel, (set(), set()))[1]

    def groups_for_channel(self, channel: str) -> set[str]:
        """Return the names of all groups that have at least one script in `channel`."""
        allowed = self.commands_for_channel(channel) | self.ambient_commands_for_channel(channel)
        result: set[str] = set()
        for group_name, members in self.groups.items():
            if members & allowed:
                result.add(group_name)
        return result

    # ── Operator resolution ────────────────────────────────────────────────

    def is_operator(
        self,
        username: str,
        is_mod: bool = False,
        is_broadcaster: bool = False,
    ) -> bool:
        """
        Return True if the user matches any operator spec in bot.operators.

        Args:
            username:       Twitch login name (lowercase).
            is_mod:         True if the user has a moderator badge.
            is_broadcaster: True if the user has a broadcaster badge.
        """
        for spec in self.bot.operators:
            if spec == "@mods" and is_mod:
                return True
            if spec == "@streamer" and is_broadcaster:
                return True
            if spec == username:
                return True
        return False


# ── Channel entry resolver ─────────────────────────────────────────────────

def _resolve_commands(
    channel: str,
    entries: list[str],
    all_scripts: set[str],
) -> tuple[set[str], set[str]]:
    """
    Resolve a channel's command list into (normal_scripts, ambient_scripts).

    Syntax:
        "@all"       — all scripts
        "name"       — explicit script
        "#name"      — ambient script (called on every message)
        "-name"      — exclude this script
    """
    result:     set[str] = set()
    ambient:    set[str] = set()
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
                    f"channels.{channel}: ambient script {name!r} not defined in [scripts]"
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
                    f"channels.{channel}: {entry!r} not defined in [scripts]"
                )
            result.add(entry)

    result  -= exclusions
    result  -= ambient
    ambient -= exclusions

    return result, ambient
