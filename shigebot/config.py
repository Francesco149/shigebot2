"""
shigebot/config.py — configuration loading.

Canonical TOML format (see shigebot.toml.example for a full annotated file):

    [bot]
    nick      = "shigebot"
    bot_id    = "123456789"
    prefix    = "!"
    operators = ["painketsu", "@mods", "@streamer"]

    # HTTP API for external message injection (0 = disabled)
    http_api_port = 8765

    [aliases]
    official = "github:Francesco149/shigebot-scripts:v2/"

    [channel_operators]
    mychannel = ["extra_mod", "-@mods"]

    [groups]
    games = ["slots", "rr", "fish", "trivia", "mirage", "bank"]
    fun   = ["8ball", "flip", "slap", "pepe", "4/4"]

    [triggers]
    "stream.online"    = ["announce_live"]
    "stream.offline"   = ["announce_offline"]
    "channel.follow"   = ["follow"]
    "channel.raid"     = ["raid"]
    "channel.ad_break" = ["ad_break"]

    [channels]
    mychannel = ["@all", "#lurk", "#logs", "-ratelimit"]

    [scripts]
    hi            = "official:hi.py"
    announce_live = "github:owner/repo:scripts/announce.py@main"
    lurk          = "https://gist.github.com/..."

    [script_options.lurk]
    worker_count = 3
    queue_size   = 20
"""
from __future__ import annotations

import os
import re
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


# ── Alias resolution ───────────────────────────────────────────────────────

_ALIAS_NAME_RE    = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')
_RESERVED_SCHEMES = frozenset({"https", "http", "github"})


def _validate_alias_name(name: str) -> None:
    if not _ALIAS_NAME_RE.match(name):
        raise ValueError(
            f"Alias name {name!r} is invalid — must start with a letter and "
            "contain only alphanumeric characters and underscores."
        )
    if name in _RESERVED_SCHEMES:
        raise ValueError(
            f"Alias name {name!r} conflicts with a reserved URL scheme. "
            f"Reserved: {sorted(_RESERVED_SCHEMES)}"
        )


def _resolve_url(url: str, aliases: dict[str, str]) -> str:
    if ":" not in url:
        return url
    prefix, rest = url.split(":", 1)
    if prefix in aliases:
        return aliases[prefix] + rest
    return url


# ── Operator spec helpers ──────────────────────────────────────────────────

_OPERATOR_SPECIALS = frozenset({"@mods", "@streamer"})


def _validate_operator_spec(spec: str, location: str) -> None:
    bare = spec.lstrip("-")
    if bare.startswith("@") and bare not in _OPERATOR_SPECIALS:
        raise ValueError(
            f"{location}: invalid operator spec {spec!r}. "
            f"Special @-values: {sorted(_OPERATOR_SPECIALS)}"
        )


def _normalise_operator(spec: str) -> str:
    if spec.startswith("-"):
        inner = spec[1:]
        return "-" + (inner if inner.startswith("@") else inner.lower())
    return spec if spec.startswith("@") else spec.lower()


# ── BotConfig ─────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    nick:    str
    bot_id:  str

    prefix:      str  = "!"
    working_dir: Path = field(default_factory=lambda: Path("/var/lib/shigebot/scripts"))
    operators:   list[str] = field(default_factory=list)

    gist_refresh_interval: int   = 300
    script_timeout:        int   = 10
    script_preamble:       str   = ""

    refresh_user_limit:  int   = 10
    refresh_user_window: float = 60.0

    rate_limit_window:           float = 30.0
    rate_limit_non_elevated_max: int   = 18
    rate_limit_elevated_max:     int   = 95

    worker_max_invocations: int = 100
    worker_idle_timeout:    int = 300
    worker_max_total:       int = 200
    worker_count:           int = 1
    worker_queue_size:      int = 3
    ambient_queue_size:     int = 0

    watchdog_timeout: int = 300

    # HTTP API for external message injection (0 = disabled)
    # Secret is read from SHIGEBOT_HTTP_SECRET env var.
    http_api_port: int = 0


# ── Config ─────────────────────────────────────────────────────────────────

@dataclass
class Config:
    bot: BotConfig

    channels:          dict[str, tuple[set[str], set[str]]] = field(default_factory=dict)
    scripts:           dict[str, str]                       = field(default_factory=dict)
    aliases:           dict[str, str]                       = field(default_factory=dict)
    groups:            dict[str, frozenset[str]]            = field(default_factory=dict)
    script_groups:     dict[str, set[str]]                  = field(default_factory=dict)
    triggers:          dict[str, list[str]]                 = field(default_factory=dict)
    script_options:    dict[str, dict[str, int]]            = field(default_factory=dict)
    channel_operators: dict[str, list[str]]                 = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)

        raw_bot = data.get("bot", {})

        # ── Aliases ───────────────────────────────────────────────────────
        aliases: dict[str, str] = {}
        for name, base in data.get("aliases", {}).items():
            _validate_alias_name(name)
            if not isinstance(base, str):
                raise ValueError(f"aliases.{name} must be a string")
            aliases[name] = base

        # ── Bot core ──────────────────────────────────────────────────────
        raw_operators = raw_bot.get("operators", [])
        for op in raw_operators:
            _validate_operator_spec(op, "[bot].operators")

        bot = BotConfig(
            nick       = raw_bot["nick"],
            bot_id     = raw_bot["bot_id"],
            prefix     = raw_bot.get("prefix", "!"),
            working_dir = Path(raw_bot.get("working_dir", "/var/lib/shigebot/scripts")),
            operators  = [_normalise_operator(op) for op in raw_operators],
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
            http_api_port          = raw_bot.get("http_api_port", 0),
        )

        # ── Scripts ───────────────────────────────────────────────────────
        raw_scripts: dict[str, str] = {}
        for name, url in data.get("scripts", {}).items():
            if name.startswith(("-", "@", "#")):
                raise ValueError(f"Script name {name!r} starts with a reserved character.")
            if not isinstance(url, str):
                raise ValueError(f"scripts.{name} must be a URL string")
            raw_scripts[name] = _resolve_url(url, aliases)

        all_scripts = set(raw_scripts)

        # ── Groups ────────────────────────────────────────────────────────
        groups: dict[str, frozenset[str]] = {}
        script_groups: dict[str, set[str]] = {}

        for group_name, members in data.get("groups", {}).items():
            if not isinstance(members, list):
                raise ValueError(f"groups.{group_name} must be a list")
            for m in members:
                if m not in all_scripts:
                    raise ValueError(f"groups.{group_name}: {m!r} not defined in [scripts]")
            groups[group_name] = frozenset(members)
            for m in members:
                script_groups.setdefault(m, set()).add(group_name)

        # ── Triggers ──────────────────────────────────────────────────────
        _supported = frozenset({
            "stream.online",
            "stream.offline",
            "channel.follow",
            "channel.raid",
            "channel.ad_break",
        })
        triggers: dict[str, list[str]] = {}

        for event_type, scripts in data.get("triggers", {}).items():
            if event_type not in _supported:
                raise ValueError(
                    f"Unsupported trigger {event_type!r}. Supported: {sorted(_supported)}"
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

        # ── Channel operators ─────────────────────────────────────────────
        channel_operators: dict[str, list[str]] = {}
        for ch, entries in data.get("channel_operators", {}).items():
            if not isinstance(entries, list):
                raise ValueError(f"channel_operators.{ch} must be a list")
            normalised = []
            for entry in entries:
                _validate_operator_spec(entry, f"channel_operators.{ch}")
                normalised.append(_normalise_operator(entry))
            channel_operators[ch] = normalised

        # ── Script options ────────────────────────────────────────────────
        script_options: dict[str, dict[str, int]] = {}
        _valid_opts = frozenset({"worker_count", "queue_size"})

        for name, opts in data.get("script_options", {}).items():
            if not isinstance(opts, dict):
                raise ValueError(f"script_options.{name} must be a table")
            if name not in all_scripts:
                raise ValueError(f"script_options.{name}: not defined in [scripts]")
            unknown = set(opts) - _valid_opts
            if unknown:
                raise ValueError(
                    f"script_options.{name}: unknown keys {unknown}. "
                    f"Valid: {sorted(_valid_opts)}"
                )
            script_options[name] = {k: int(v) for k, v in opts.items()}

        return cls(
            bot               = bot,
            channels          = channels,
            scripts           = raw_scripts,
            aliases           = aliases,
            groups            = groups,
            script_groups     = script_groups,
            triggers          = triggers,
            script_options    = script_options,
            channel_operators = channel_operators,
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
        allowed = self.commands_for_channel(channel) | self.ambient_commands_for_channel(channel)
        return {
            name for name, members in self.groups.items()
            if members & allowed
        }

    def is_operator(
        self,
        username:       str,
        channel:        str | None = None,
        is_mod:         bool = False,
        is_broadcaster: bool = False,
    ) -> bool:
        """
        Return True if the user matches any operator spec effective for `channel`.

        Resolution:
          1. Start from the global [bot].operators list.
          2. Apply [channel_operators.<channel>] modifiers:
               - Entries without '-' prefix: add to the effective list.
               - Entries with '-' prefix: remove that spec from the list.
          3. Check username / is_mod / is_broadcaster against the result.
        """
        specs: list[str] = list(self.bot.operators)

        if channel:
            for entry in self.channel_operators.get(channel, []):
                if entry.startswith("-"):
                    remove = entry[1:]
                    if remove in specs:
                        specs.remove(remove)
                else:
                    if entry not in specs:
                        specs.append(entry)

        for spec in specs:
            if spec == "@mods" and is_mod:
                return True
            if spec == "@streamer" and is_broadcaster:
                return True
            if spec == username:
                return True
        return False


# ── Channel entry resolver ─────────────────────────────────────────────────

def _resolve_commands(
    channel:     str,
    entries:     list[str],
    all_scripts: set[str],
) -> tuple[set[str], set[str]]:
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
                    f"channels.{channel}: ambient script {name!r} not in [scripts]"
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
                    f"channels.{channel}: {entry!r} not in [scripts]"
                )
            result.add(entry)

    result  -= exclusions
    result  -= ambient
    ambient -= exclusions
    return result, ambient
