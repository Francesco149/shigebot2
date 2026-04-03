"""
shigebot.py — v2 runtime module. (SPEC 2.2)

Place this file in working_dir so all v2 scripts can `import shigebot as sb`.

Key rule: sb.ctx / sb.data / sb.channel / sb.global_ are NOT initialised on
import. They are populated by sb._reset() before each main() call.
Never access them at module level.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
from typing import Any, Iterator, Literal

# ── Constants ─────────────────────────────────────────────────────────────

_CTX_ENV     = "SHIGEBOT_CTX"
_CHANNEL_DB  = "channel.db"
_GLOBAL_DB   = "global.db"
_ACTION_BYTE = "\x00"

_KV_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    namespace  TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS kv_ns ON kv (namespace);
"""

AnnounceColor = Literal["blue", "green", "orange", "purple", "primary"]

# ── Context ───────────────────────────────────────────────────────────────

class Reply:
    __slots__ = ("user", "message", "message_id")

    def __init__(self, data: dict) -> None:
        self.user:       str = data.get("user", "")
        self.message:    str = data.get("message", "")
        self.message_id: str = data.get("message_id", "")


class Context:
    """
    Rich context for the current script invocation.
    Populated by sb._reset() before each main() call.
    Do NOT access any field at module level.
    """
    __slots__ = (
        "user", "channel", "args", "msg_id", "timestamp",
        "prefix", "bot_nick", "is_ambient", "is_operator", "script_name",
        "channel_dir", "global_dir", "reply", "event_data",
    )

    def __init__(self, data: dict) -> None:
        self.user:        str        = data.get("user", "")
        self.channel:     str        = data.get("channel", "")
        self.args:        list[str]  = data.get("args", [])
        self.msg_id:      str        = data.get("msg_id", "")
        self.timestamp:   float      = data.get("timestamp", time.time())
        self.prefix:      str        = data.get("prefix", "!")
        self.bot_nick:    str        = data.get("bot_nick", "")
        self.is_ambient:  bool       = data.get("is_ambient", False)
        self.is_operator: bool       = data.get("is_operator", False)
        self.script_name: str        = data.get("script_name", "")
        self.channel_dir: str        = data.get("channel_dir", str(pathlib.Path.cwd()))
        self.global_dir:  str        = data.get("global_dir", str(pathlib.Path.cwd()))
        # Structured payload for trigger scripts (empty for command invocations).
        # Examples:
        #   channel.follow:   {"from_user": "alice", "from_user_display": "Alice"}
        #   channel.raid:     {"from_user": "bob", "viewer_count": 42}
        #   channel.ad_break: {"duration": 90, "is_automatic": False}
        self.event_data:  dict       = data.get("event_data", {})
        reply_raw = data.get("reply")
        self.reply: Reply | None     = Reply(reply_raw) if reply_raw else None


# ── SQLite connection pool ─────────────────────────────────────────────────

_thread_local = threading.local()


def _open_conn(path: str) -> sqlite3.Connection:
    attr = f"_conn_{path}"
    conn: sqlite3.Connection | None = getattr(_thread_local, attr, None)
    if conn is None:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_KV_SCHEMA)
        conn.commit()
        setattr(_thread_local, attr, conn)
    return conn


# ── Store ─────────────────────────────────────────────────────────────────

class _Transaction:
    def __init__(self, store: "Store") -> None:
        self._conn = store._conn()
        self._ns   = store._ns

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?",
            (self._ns, key),
        ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def set(self, key: str, value: Any) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO kv (namespace, key, value, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(namespace, key)
            DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (self._ns, key, json.dumps(value), now),
        )

    def delete(self, key: str) -> None:
        self._conn.execute(
            "DELETE FROM kv WHERE namespace=? AND key=?", (self._ns, key)
        )

    def incr(
        self, key: str, amount: int | float = 1, default: int | float = 0
    ) -> int | float:
        current = self.get(key, default)
        new_val = current + amount
        self.set(key, new_val)
        return new_val


class Store:
    """
    Thread-safe key-value store backed by SQLite.

    Values must be JSON-serialisable (int, float, str, bool, None, list, dict).
    Do NOT access the underlying kv table via raw SQL — use sb.db() for
    script-owned tables instead.
    """

    def __init__(self, db_path: str, namespace: str) -> None:
        self._path = db_path
        self._ns   = namespace

    def _conn(self) -> sqlite3.Connection:
        return _open_conn(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?",
            (self._ns, key),
        ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def all(self) -> dict[str, Any]:
        rows = self._conn().execute(
            "SELECT key, value FROM kv WHERE namespace=?", (self._ns,)
        ).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def set(self, key: str, value: Any) -> None:
        now  = int(time.time())
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO kv (namespace, key, value, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(namespace, key)
            DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (self._ns, key, json.dumps(value), now),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        conn = self._conn()
        conn.execute(
            "DELETE FROM kv WHERE namespace=? AND key=?", (self._ns, key)
        )
        conn.commit()

    def incr(
        self, key: str, amount: int | float = 1, default: int | float = 0
    ) -> int | float:
        current = self.get(key, default)
        new_val = current + amount
        self.set(key, new_val)
        return new_val

    @contextlib.contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        """
        Atomic multi-key transaction. Commits on clean exit, rolls back on
        exception.

        Example::

            with sb.channel.transaction() as tx:
                a = tx.get("bank:balance:alice", 0)
                tx.set("bank:balance:alice", a - 100)
                tx.set("bank:balance:bob", tx.get("bank:balance:bob", 0) + 100)
        """
        conn = self._conn()
        conn.execute("BEGIN EXCLUSIVE")
        tx = _Transaction(self)
        try:
            yield tx
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ── Raw SQLite access ─────────────────────────────────────────────────────

@contextlib.contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """
    Raw SQLite access to the channel database.
    Commits on clean exit, rolls back on exception.

    Do NOT touch the kv table or any kv_-prefixed tables — they are
    owned by the runtime.
    """
    _ensure_ctx()
    path = str(pathlib.Path(ctx.channel_dir) / _CHANNEL_DB)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def global_db() -> Iterator[sqlite3.Connection]:
    """Raw SQLite access to the global database."""
    _ensure_ctx()
    path = str(pathlib.Path(ctx.global_dir) / _GLOBAL_DB)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Output helpers ────────────────────────────────────────────────────────

def say(text: str) -> None:
    """Send a plain chat message."""
    print(text)


def sayf(fmt: str, *args: Any) -> None:
    """Format and send a plain chat message."""
    print(fmt.format(*args))


def _action(**kwargs: Any) -> None:
    """Emit a \\x00-prefixed JSON action line."""
    print(_ACTION_BYTE + json.dumps(kwargs), flush=True)


def reply(text: str, to: str | None = None) -> None:
    """
    Reply to the message that triggered this invocation (or to `to` msg_id).
    Includes `user` so the bot can fall back to an @-mention if needed.
    """
    _ensure_ctx()
    _action(action="reply", to=to or ctx.msg_id, user=ctx.user, text=text)


def announce(text: str, color: AnnounceColor | None = None) -> None:
    """
    Send a channel announcement.

    `color` is optional. Valid values: "blue", "green", "orange", "purple",
    "primary". Omit for the default channel color.
    """
    kwargs: dict[str, Any] = {"action": "announce", "text": text}
    if color is not None:
        kwargs["color"] = color
    _action(**kwargs)


def me(text: str) -> None:
    """Send a /me action message."""
    _action(action="me", text=text)


# ── Mod action helpers ────────────────────────────────────────────────────
# These go through the action protocol and are handled by the bot via the
# Twitch API. They cannot be spoofed through sb.say() / plain stdout.

def shoutout(target: str) -> None:
    """
    Send a Twitch shoutout to `target` (login name or display name).
    Requires the bot to have moderator:manage:shoutouts scope.
    """
    _action(action="shoutout", target=target)


def ban(target: str, reason: str = "") -> None:
    """
    Permanently ban `target` from the channel.
    Requires the bot to have moderator:manage:banned_users scope.
    """
    _action(action="ban", target=target, reason=reason)


def timeout(target: str, duration: int = 600, reason: str = "") -> None:
    """
    Timeout `target` for `duration` seconds (default 600 = 10 minutes).
    Requires the bot to have moderator:manage:banned_users scope.
    """
    _action(action="timeout", target=target, duration=duration, reason=reason)


def unban(target: str) -> None:
    """
    Unban or remove timeout for `target`.
    Requires the bot to have moderator:manage:banned_users scope.
    """
    _action(action="unban", target=target)


# ── Migration helpers ─────────────────────────────────────────────────────

class _Migrate:
    """One-shot helpers for importing v1 pickle state."""

    def from_pickle(
        self,
        store: Store,
        key: str,
        filename: str,
        transform: Any = None,
    ) -> bool:
        sentinel = f"__migrated__{key}"
        if store.get(sentinel) is True:
            return False
        p = pathlib.Path(filename)
        if not p.exists():
            return False
        try:
            import pickle
            value = pickle.loads(p.read_bytes())
            if transform is not None:
                value = transform(value)
            store.set(key, value)
            store.set(sentinel, True)
            return True
        except Exception:
            return False

    def pickles_in(self, directory: str) -> dict[str, Any]:
        import pickle
        result: dict[str, Any] = {}
        for p in pathlib.Path(directory).glob("*.pickle"):
            try:
                result[p.stem] = pickle.loads(p.read_bytes())
            except Exception:
                pass
        return result


migrate = _Migrate()


# ── Internal reset (called by worker before each main()) ──────────────────

ctx:     Context | None = None
data:    Store   | None = None
channel: Store   | None = None
global_: Store   | None = None


def _ensure_ctx() -> None:
    if ctx is None:
        raise RuntimeError(
            "sb.ctx is not initialised. "
            "Do not access sb.ctx / sb.data / sb.channel / sb.global_ "
            "at module level — only inside main()."
        )


def _reset(ctx_blob: dict) -> None:
    """Called by the worker before each main() invocation."""
    global ctx, data, channel, global_

    ctx = Context(ctx_blob)

    channel_db     = str(pathlib.Path(ctx.channel_dir) / _CHANNEL_DB)
    global_db_path = str(pathlib.Path(ctx.global_dir)  / _GLOBAL_DB)

    pathlib.Path(ctx.channel_dir).mkdir(parents=True, exist_ok=True)

    ns      = f"script:{ctx.script_name}" if ctx.script_name else "script:unknown"
    data    = Store(channel_db,     ns)
    channel = Store(channel_db,     "shared")
    global_ = Store(global_db_path, "shared")


# ── Bootstrap for local testing ───────────────────────────────────────────

def _bootstrap_from_env() -> None:
    argv0 = sys.argv[0] if sys.argv else ""
    raw   = os.environ.get(_CTX_ENV)
    if raw:
        _reset(json.loads(raw))
        return

    _reset({
        "user":        os.environ.get("NICK", ""),
        "channel":     os.environ.get("CHANNEL", ""),
        "args":        sys.argv[1:],
        "msg_id":      os.environ.get("MSG_ID", ""),
        "prefix":      os.environ.get("PREFIX", "!"),
        "bot_nick":    os.environ.get("BOT_NICK", ""),
        "is_operator": os.environ.get("IS_OPERATOR", "0") == "1",
        "channel_dir": str(pathlib.Path.cwd()),
        "global_dir":  str(pathlib.Path(argv0).parent) if argv0 else str(pathlib.Path.cwd()),
        "script_name": pathlib.Path(argv0).stem if argv0 else "",
        "event_data":  {},
        "reply": {
            "user":       os.environ.get("REPLY_TO_USER", ""),
            "message":    os.environ.get("REPLY_TO_MESSAGE", ""),
            "message_id": os.environ.get("REPLY_TO_MESSAGE_ID", ""),
        } if os.environ.get("REPLY_TO_USER") else None,
    })


_bootstrap_from_env()
