"""
shigebot.py — v2 runtime module. (SPEC 2.1)

Place this file in working_dir so all v2 scripts can `import shigebot as sb`.

Key change from 2.0: sb.ctx / sb.data / sb.channel / sb.global_ are NOT
initialised on import. They are populated by sb._reset() immediately before
each main() call. Never access them at module level.
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
from typing import Any, Iterator

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
    Populated by sb._reset() before each main() call — do not access
    any sb.ctx field at module level.
    """
    __slots__ = (
        "user", "channel", "args", "msg_id", "timestamp",
        "prefix", "bot_nick", "is_ambient", "script_name",
        "channel_dir", "global_dir", "reply",
    )

    def __init__(self, data: dict) -> None:
        self.user:        str       = data.get("user", "")
        self.channel:     str       = data.get("channel", "")
        self.args:        list[str] = data.get("args", [])
        self.msg_id:      str       = data.get("msg_id", "")
        self.timestamp:   float     = data.get("timestamp", time.time())
        self.prefix:      str       = data.get("prefix", "!")
        self.bot_nick:    str       = data.get("bot_nick", "")
        self.is_ambient:  bool      = data.get("is_ambient", False)
        self.script_name: str       = data.get("script_name", "")
        self.channel_dir: str       = data.get("channel_dir", str(pathlib.Path.cwd()))
        self.global_dir:  str       = data.get("global_dir", str(pathlib.Path.cwd()))
        reply_raw = data.get("reply")
        self.reply: Reply | None    = Reply(reply_raw) if reply_raw else None


# ── SQLite connection pool (thread-local, one conn per db path) ───────────

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
            "DELETE FROM kv WHERE namespace=? AND key=?",
            (self._ns, key),
        )

    def incr(self, key: str, amount: int | float = 1, default: int | float = 0) -> int | float:
        current = self.get(key, default)
        new_val = current + amount
        self.set(key, new_val)
        return new_val


class Store:
    """
    Thread-safe key-value store backed by a SQLite kv table.

    Values must be JSON-serialisable (int, float, str, bool, None, list, dict).
    Do NOT access the underlying kv table via raw SQL — use sb.db() for
    script-owned tables instead.
    """

    def __init__(self, db_path: str, namespace: str) -> None:
        self._path = db_path
        self._ns   = namespace

    def _conn(self) -> sqlite3.Connection:
        return _open_conn(self._path)

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn().execute(
            "SELECT value FROM kv WHERE namespace=? AND key=?",
            (self._ns, key),
        ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def all(self) -> dict[str, Any]:
        rows = self._conn().execute(
            "SELECT key, value FROM kv WHERE namespace=?",
            (self._ns,),
        ).fetchall()
        return {k: json.loads(v) for k, v in rows}

    # ── Write ─────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any) -> None:
        now = int(time.time())
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
        conn.execute("DELETE FROM kv WHERE namespace=? AND key=?", (self._ns, key))
        conn.commit()

    def incr(self, key: str, amount: int | float = 1, default: int | float = 0) -> int | float:
        current = self.get(key, default)
        new_val = current + amount
        self.set(key, new_val)
        return new_val

    # ── Transactions ──────────────────────────────────────────────────────

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

    Use this for script-owned tables (e.g. fish_catalogue, lurk_messages).
    Do NOT touch the kv table or any kv_-prefixed tables — they are reserved
    by the runtime.
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
    """
    Raw SQLite access to the global database.
    Commits on clean exit, rolls back on exception.
    """
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
    """Emit a \x00-prefixed JSON action line."""
    print(_ACTION_BYTE + json.dumps(kwargs), flush=True)


def reply(text: str, to: str | None = None) -> None:
    """
    Reply to the message that triggered this invocation, or to `to` (msg_id).
    """
    _ensure_ctx()
    _action(action="reply", to=to or ctx.msg_id, text=text)


def announce(text: str) -> None:
    """Send a channel announcement."""
    _action(action="announce", text=text)


def me(text: str) -> None:
    """Send a /me action message."""
    _action(action="me", text=text)


# ── Migration helpers ─────────────────────────────────────────────────────

class _Migrate:
    """
    One-shot helpers for importing v1 pickle state.

    Each migration runs exactly once; completion is tracked via a sentinel key
    in the target store. Safe to call unconditionally on every invocation.
    """

    def from_pickle(
        self,
        store: Store,
        key: str,
        filename: str,
        transform: Any = None,
    ) -> bool:
        """
        If `key` does not exist in `store`, load it from `filename` (.pickle).
        `transform` is an optional callable applied to the loaded value.
        Returns True if migrated, False if already done or file missing.
        """
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
        """
        Load all *.pickle files in `directory`.
        Returns {stem: value}. Files that fail to load are skipped.
        """
        import pickle
        result: dict[str, Any] = {}
        for p in pathlib.Path(directory).glob("*.pickle"):
            try:
                result[p.stem] = pickle.loads(p.read_bytes())
            except Exception:
                pass
        return result


migrate = _Migrate()


# ── Internal reset — called by the worker before each main() ──────────────

# Module-level handles — None until _reset() is called.
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
    """
    Called by the worker process before each main() invocation.
    Re-initialises context and store handles for the new job.
    Not part of the public script API.
    """
    global ctx, data, channel, global_

    ctx = Context(ctx_blob)

    channel_db = str(pathlib.Path(ctx.channel_dir) / _CHANNEL_DB)
    global_db_path = str(pathlib.Path(ctx.global_dir) / _GLOBAL_DB)

    # Ensure channel_dir exists (first time a script runs in a new channel)
    pathlib.Path(ctx.channel_dir).mkdir(parents=True, exist_ok=True)

    script_ns = f"script:{ctx.script_name}" if ctx.script_name else "script:unknown"
    data    = Store(channel_db,     script_ns)
    channel = Store(channel_db,     "shared")
    global_ = Store(global_db_path, "shared")


# ── Bootstrap for local testing (no worker process) ───────────────────────

def _bootstrap_from_env() -> None:
    """
    Called when shigebot is imported outside the worker (manual testing).
    Builds context from legacy env vars + sys.argv.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    raw = os.environ.get(_CTX_ENV)
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
        "channel_dir": str(pathlib.Path.cwd()),
        "global_dir":  str(pathlib.Path(argv0).parent) if argv0 else str(pathlib.Path.cwd()),
        "script_name": pathlib.Path(argv0).stem if argv0 else "",
        "reply": {
            "user":       os.environ.get("REPLY_TO_USER", ""),
            "message":    os.environ.get("REPLY_TO_MESSAGE", ""),
            "message_id": os.environ.get("REPLY_TO_MESSAGE_ID", ""),
        } if os.environ.get("REPLY_TO_USER") else None,
    })


# Initialise from environment on import (covers local testing and v1 runner
# fallback). The worker process calls _reset() again before each main(),
# overwriting this initial state.
_bootstrap_from_env()
