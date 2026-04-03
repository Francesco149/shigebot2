# shigebot v2 script API — specification

> **This document is the single source of truth for the v2 API.**
> All implementation changes must be reflected here first.

**Spec version: 2.2**

---

## 1. Script identification

```python
# shigebot: v2
```

Must be the first line. Scripts without it are treated as v1.

---

## 2. Entry point

Every v2 script must define `main()`. Module-level code runs once at worker
startup; `main()` runs once per invocation.

```python
# shigebot: v2
import shigebot as sb

def main():
    sb.say(f"hello {sb.ctx.user}")
```

**Never access `sb.ctx`, `sb.data`, `sb.channel`, or `sb.global_` at module
level.** They are populated by `sb._reset()` before each `main()` call.

Module-level code is fine for: imports, constants, helper functions.

---

## 3. Import pre-loading

Because the worker imports the script module once at startup (via
`spec.loader.exec_module`), **all top-level `import` statements execute at
worker startup**, not per-invocation. Imported modules are cached in
`sys.modules` and reused across every `main()` call.

```python
# This import runs ONCE when the worker starts, not once per !fish:
import numpy as np
import pandas as pd
```

### Preamble

`script_preamble` in `[bot]` is exec'd in the worker *before* the script
module is imported. It does not make names available inside scripts; its
purpose is:

1. **Cross-script pre-warming** — warm packages shared across multiple scripts
   so no single script pays the cold-start cost.
2. **Dependency validation** — fail fast if a required package is missing.
3. **Global configuration** — set env vars or modify `sys.path`.

---

## 4. Output protocol

### 4.1 Plain chat lines

Any stdout line not starting with `\x00` is sent as a chat message.

```python
print("hello chat")
sb.say("hello chat")   # identical
```

| Limit | Value |
|-------|-------|
| Maximum lines per invocation | 10 |
| Maximum characters per line | 350 |

**Safety:** Lines starting with `/` are silently dropped by the manager and
logged as a warning. Use the dedicated `sb.me()`, `sb.ban()`, `sb.timeout()`
etc. helpers instead. This prevents user-controlled input from being crafted
into Twitch chat commands.

### 4.2 Action lines

Lines beginning with `\x00` are JSON action descriptors — never written
directly by scripts, only via `sb.*` helpers.

```
\x00{"action": "reply",    "to": "<msg_id>", "user": "<login>", "text": "..."}
\x00{"action": "announce", "text": "...", "color": "blue"}     # color optional
\x00{"action": "me",       "text": "..."}
\x00{"action": "shoutout", "target": "<login>"}
\x00{"action": "ban",      "target": "<login>", "reason": "..."}
\x00{"action": "timeout",  "target": "<login>", "duration": 600, "reason": "..."}
\x00{"action": "unban",    "target": "<login>"}
\x00{"action": "done",     "job_id": "<id>"}                   # worker only
\x00{"action": "error",    "job_id": "<id>", "msg": "..."}     # worker only
```

`done` and `error` are emitted by the worker process, not by scripts.

### 4.3 Output helpers

```python
sb.say(text)                          # plain chat message
sb.sayf(fmt, *args)                   # fmt.format(*args)
sb.reply(text, to=None)               # Twitch reply; falls back to @mention
sb.announce(text, color=None)         # channel announcement
                                      # color: "blue"|"green"|"orange"|"purple"|"primary"
sb.me(text)                           # /me action message

# Mod actions — go through the action protocol, cannot be spoofed via say()
sb.shoutout(target)                   # send a Twitch shoutout
sb.ban(target, reason="")             # permanently ban a user
sb.timeout(target, duration=600,      # timeout for `duration` seconds
           reason="")
sb.unban(target)                      # unban / remove timeout
```

### 4.4 Output delivery guarantee

The dispatcher does not start the next job until all output from the current
job has been consumed and sent. This prevents output from consecutive
invocations being interleaved and stops stale lines building up behind the
rate limiter.

---

## 5. Context

### 5.1 Job descriptor (worker stdin JSON)

```jsonc
{
  "job_id": "uuid...",
  "ctx": {
    "user":        "alice",
    "channel":     "mychannel",
    "args":        ["arg1"],
    "msg_id":      "uuid...",
    "timestamp":   1712345678.0,
    "prefix":      "!",
    "bot_nick":    "shigebot",
    "is_ambient":  false,
    "is_operator": false,
    "script_name": "8ball",
    "channel_dir": "/var/lib/shigebot/scripts/mychannel",
    "global_dir":  "/var/lib/shigebot/scripts",
    "event_data":  {},
    "reply": {"user": "bob", "message": "...", "message_id": "uuid..."} | null
  }
}
```

### 5.2 `sb.ctx` fields

```python
sb.ctx.user           # str  — Twitch login
sb.ctx.channel        # str  — channel name (no #)
sb.ctx.args           # list[str]
sb.ctx.msg_id         # str
sb.ctx.timestamp      # float (Unix UTC)
sb.ctx.prefix         # str
sb.ctx.bot_nick       # str
sb.ctx.is_ambient     # bool
sb.ctx.is_operator    # bool
sb.ctx.script_name    # str
sb.ctx.channel_dir    # str (absolute path)
sb.ctx.global_dir     # str (absolute path)
sb.ctx.event_data     # dict — structured event payload (see §5.3)
sb.ctx.reply          # Reply | None
sb.ctx.reply.user
sb.ctx.reply.message
sb.ctx.reply.message_id
```

### 5.3 `event_data` for trigger scripts

`event_data` is populated for trigger script invocations; it is an empty
dict for regular command and ambient invocations.

| Event type | Keys |
|------------|------|
| `stream.online` | `stream_type: str` |
| `stream.offline` | _(empty)_ |
| `channel.follow` | `from_user: str`, `from_user_display: str` |
| `channel.raid` | `from_user: str`, `from_user_display: str`, `viewer_count: int` |
| `channel.ad_break` | `duration: int` (seconds), `is_automatic: bool` |

### 5.4 Fallback for local testing

When `SHIGEBOT_CTX` is absent, `shigebot.py` builds context from legacy env
vars (`NICK`, `CHANNEL`, `IS_OPERATOR`, etc.) and `sys.argv`.

---

## 6. Data stores

### 6.1 Scopes

| Variable | Database | Namespace |
|----------|----------|-----------|
| `sb.data` | `{channel_dir}/channel.db` | `script:{script_name}` |
| `sb.channel` | `{channel_dir}/channel.db` | `shared` |
| `sb.global_` | `{global_dir}/global.db` | `shared` |

### 6.2 Store API

```python
store.get(key, default=None) -> Any
store.set(key, value)                   # JSON-serialisable
store.delete(key)
store.all() -> dict
store.incr(key, amount=1, default=0) -> int | float
```

### 6.3 Transactions

```python
with sb.channel.transaction() as tx:
    a = tx.get("bank:balance:alice", 0)
    tx.set("bank:balance:alice", a - 100)
    tx.set("bank:balance:bob", tx.get("bank:balance:bob", 0) + 100)
```

### 6.4 Raw SQLite

```python
with sb.db() as conn:           # channel DB
    conn.execute("CREATE TABLE IF NOT EXISTS fish_catalogue ...")

with sb.global_db() as conn:    # global DB
    ...
```

**Reserved:** the `kv` table and any table/index prefixed with `kv_` are
owned by the runtime. Use a script-name prefix for your own tables.

### 6.5 Migration helpers

```python
sb.migrate.from_pickle(store, key, filename, transform=None) -> bool
sb.migrate.pickles_in(directory) -> dict[str, Any]
```

---

## 7. Worker model

### 7.1 Overview

One persistent Python process per `(script, channel)` pair. The worker imports
the script once and calls `main()` per job.

### 7.2 Worker lifecycle

1. Spawn with `PYTHONPATH` prepended so `import shigebot` finds
   `working_dir/shigebot.py` (not the bot package).
2. Execute `SHIGEBOT_PREAMBLE` if set.
3. Import the script module (once — module-level code here).
4. Job loop:
   a. `sb._reset(ctx_blob)` — fresh context and store handles.
   b. `script.main()`.
   c. Emit `error` action on exception.
   d. Always emit `done` and flush.
5. On idle timeout: process exits cleanly. A `_monitor` coroutine running in
   the bot detects the exit immediately (sets `alive=False`), so the next job
   goes through the respawn path with the job still in hand rather than being
   dropped.
6. On max_invocations: same clean exit and respawn.
7. On crash: job dropped (no requeue), worker respawned.

### 7.3 Queue and drop policy

- Queue full + command → busy reply (10s per-user cooldown) + drop.
- Queue full + ambient → drop silently.

### 7.4 Global process cap

`worker_max_total` in `[bot]` is a hard cap on total live workers.

---

## 8. Configuration

### 8.1 Bot defaults

```toml
[bot]
worker_max_invocations = 100
worker_idle_timeout    = 300
worker_max_total       = 200
worker_count           = 1
worker_queue_size      = 3
ambient_queue_size     = 0
watchdog_timeout       = 300
```

### 8.2 Per-script overrides

```toml
[script_options.lurk]
worker_count = 3
queue_size   = 20

[script_options.logs]
queue_size = 100
```

### 8.3 Source aliases

```toml
[aliases]
official = "github:Francesco149/shigebot-scripts:v2/"

[scripts]
hi   = "official:hi.py"          # → github:Francesco149/shigebot-scripts:v2/hi.py
lurk = "https://gist.github.com/..."
```

Alias names: alphanumeric + underscore, cannot be `https`/`http`/`github`.

### 8.4 Operators

```toml
[bot]
operators = ["alice", "@mods", "@streamer"]

[channel_operators]
# Per-channel additions and - removals
mychannel = ["bob", "-@mods"]
```

### 8.5 Event triggers

```toml
[triggers]
"stream.online"    = ["announce_live"]
"stream.offline"   = ["announce_offline"]
"channel.follow"   = ["follow"]        # moderator:read:followers scope required
"channel.raid"     = ["raid"]
"channel.ad_break" = ["ad_break"]      # channel:read:ads scope required
```

---

## 9. Shared channel data key conventions

| Prefix | Owner | Description |
|--------|-------|-------------|
| `bank:balance:{user}` | bank, slots, rr, fish, trivia, mirage | campbucks |
| `bank:claim:{user}` | bank | next weekly claim timestamp |
| `raids:last` | raid | login of last raider |
| `rr:lock:{user}` | rr | next daily reset |
| `rr:chamber:{user}` | rr | `{chamber, pos, won_today}` |
| `rr:stats:{user}` | rr | cumulative stats |
| `slots:lock:{user}` | slots | next daily reset |
| `slots:stats:{user}` | slots | cumulative stats |
| `trivia:lock:{user}` | trivia | next daily trivia |
| `trivia:stats:{user}` | trivia | cumulative stats |
| `mirage:lock:{user}` | mirage | next daily mirage |
| `mirage:stats:{user}` | mirage | cumulative stats |
| `fish:cooldown:{user}` | fish | last cast timestamp |
| `fish:items:{user}` | fish | items inventory |
| `fish:dailybait_lock:{user}` | fish | next dailybait claim |

---

## 10. Required OAuth scopes

Run `shigebot-auth` to regenerate tokens when scopes change.

| Scope | Required for |
|-------|-------------|
| `user:read:chat` | Receiving chat messages |
| `user:write:chat` | Sending chat messages |
| `user:bot` | Bot identification |
| `moderator:manage:announcements` | `sb.announce()` |
| `moderator:read:followers` | `channel.follow` trigger |
| `channel:read:ads` | `channel.ad_break` trigger |
| `moderator:manage:shoutouts` | `sb.shoutout()` |
| `moderator:manage:banned_users` | `sb.ban()`, `sb.timeout()`, `sb.unban()` |

---

## 11. Changelog

### 2.2 (current)
- §3: Documented that top-level imports are pre-loaded at worker startup.
- §4.1: `/`-prefixed lines dropped by manager (safe say).
- §4.3: `sb.announce()` gains optional `color` parameter.
- §4.3: New mod action helpers: `sb.shoutout()`, `sb.ban()`, `sb.timeout()`, `sb.unban()`.
- §4.4: Output delivery guarantee via `drain_event`.
- §5.3: `event_data` dict in context for trigger scripts.
- §7.2: Idle-exit fix via `_monitor` coroutine (job no longer dropped on first call after idle timeout).
- §7.3: Busy reply 10-second per-user cooldown.
- §8.5: New triggers: `channel.follow`, `channel.raid`, `channel.ad_break`.
- §10: OAuth scopes table.

### 2.1
- Persistent worker pool, `main()` entry point, action protocol,
  `sb.reply/announce/me`, per-script queue config, global process cap,
  reserved `kv` table.

### 2.0
- Initial v2 spec.
