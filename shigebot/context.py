"""
shigebot/context.py — shared job context for v1 runner and v2 worker pool.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class JobContext:
    """
    Everything a script invocation needs to know about its environment.

    Build one with Shigebot._build_job_ctx() or _build_trigger_ctx(),
    never by hand inside bot.py.
    """

    # Script identity
    script_name: str
    channel:     str

    # Invoker
    user:        str
    args:        list[str]
    msg_id:      str
    timestamp:   float

    # Bot config
    prefix:      str
    bot_nick:    str

    # Flags
    is_ambient:  bool
    is_operator: bool

    # Storage paths
    channel_dir: Path
    global_dir:  Path

    # Reply context (empty strings when not a reply)
    reply_user:       str = ""
    reply_message:    str = ""
    reply_message_id: str = ""

    # Structured event payload for trigger scripts.
    # Examples:
    #   channel.follow:    {"from_user": "alice", "from_user_display": "Alice"}
    #   channel.raid:      {"from_user": "bob", "viewer_count": 42}
    #   channel.ad_break:  {"duration": 90, "is_automatic": False}
    #   stream.online:     {"stream_type": "live"}
    # Empty dict for regular command/ambient invocations.
    event_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialise for SHIGEBOT_CTX env var and worker stdin JSON."""
        return {
            "script_name": self.script_name,
            "channel":     self.channel,
            "user":        self.user,
            "args":        self.args,
            "msg_id":      self.msg_id,
            "timestamp":   self.timestamp,
            "prefix":      self.prefix,
            "bot_nick":    self.bot_nick,
            "is_ambient":  self.is_ambient,
            "is_operator": self.is_operator,
            "channel_dir": str(self.channel_dir),
            "global_dir":  str(self.global_dir),
            "event_data":  self.event_data,
            "reply": {
                "user":       self.reply_user,
                "message":    self.reply_message,
                "message_id": self.reply_message_id,
            } if self.reply_user else None,
        }

    def to_v1_env(self) -> dict[str, str]:
        """Legacy environment variables for v1 scripts."""
        return {
            "NICK":                self.user,
            "CHANNEL":             self.channel,
            "REPLY_TO_USER":       self.reply_user,
            "REPLY_TO_MESSAGE":    self.reply_message,
            "REPLY_TO_MESSAGE_ID": self.reply_message_id,
            "MSG_ID":              self.msg_id,
            "TIMESTAMP":           str(self.timestamp),
            "PREFIX":              self.prefix,
            "BOT_NICK":            self.bot_nick,
            "IS_OPERATOR":         "1" if self.is_operator else "0",
        }
