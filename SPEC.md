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
module is imported. Its purpose is:

1. **Cross-script pre-warming** — warm packages shared across multiple scripts.
2. **Dependency validation** — fail fast if a required package is missing.
3. **Global configuration** — set env vars or modify `sys.path`.

Names defined by the preamble do not leak into script namespaces. Scripts
must still use their own `import` statements.

---

## 4. Output protocol

### 4.1 Plain chat lines

Any stdout line not starting with `\x00` is sent as a chat message.

**All output must go through `sb.say()`, `sb.sayf()`, or `print()`.**
Never construct strings that start with `/` or `.` from user-controlled
input — see §4.1.1 for how the runtime handles this.

```python
sb.say("hello chat")
sb.sayf("hello {}", sb.ctx.user)
```

Limits:

| Limit | Value |
|-------|-------|
| Maximum lines per invocation | 10 |
| Maximum characters per line | 350 |

#### 4.1.1 Output sanitization

All plain chat output is sanitized before sending:

1. **CRLF stripping** — `\n` and `\r` are replaced with a space. This
   prevents newline injection (e.g. user input `"test\r\n/ban alice"` split
   into two effective messages).

2. **Command prefix replacement** — Twitch evaluates both `/` and `.` as
   chat-command prefixes (`/ban`, `.me`, etc.). Leading whitespace is
   stripped before the check so spaces can't bypass it. If the stripped text
   starts with `/` or `.`, the character is replaced with `#` and a WARNING
   is logged. The message is still sent (not dropped) so the intent is
   visible.

The same CRLF stripping is applied to text carried in action payloads
(reply, announce, me) for hygiene, though these go through the Twitch API
and are not vulnerable to chat-command injection.

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

### 4.3 Output helpers

```python
sb.say(text)                          # plain chat message
sb.sayf(fmt, *args)                   # fmt.format(*args)
sb.reply(text, to=None)               # Twitch reply; falls back to @mention
sb.announce(text, color=None)         # channel announcement
                                      # color: "blue"|"green"|"orange"|"purple"|"primary"
sb.me(text)                           # /me action message

# Mod actions — go through the action protocol, cannot be spoofed via say()
sb.shoutout(target)                   # Twitch shoutout (requires login, resolved to ID)
sb.ban(target, reason="")             # permanently ban a user
sb.timeout(target, duration=600,      # timeout for `duration` seconds
           reason="")
sb.unban(target)                      # unban / remove timeout
```

### 4.4 Output delivery guarantee

The dispatcher does not start the next job until all output from the current
job has been consumed and sent. This prevents interleaved output from command
spam and stops stale lines building up behind the rate limiter.

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

`event_data` is populated for trigger script invocations; empty dict for
regular command and ambient invocations.

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
owned by the runtime.

### 6.5 Migration helpers

```python
sb.migrate.from_pickle(store, key, filename, transform=None) -> bool
sb.migrate.pickles_in(directory) -> dict[str, Any]
```

---

## 7. Worker model

### 7.1 Overview

One persistent Python process per `(script, channel)` pair.

### 7.2 Worker lifecycle

1. Spawn with `PYTHONPATH` prepended so `import shigebot` finds
   `working_dir/shigebot.py` (not the bot package).
2. Execute `SHIGEBOT_PREAMBLE` if set.
3. Import the script module (once).
4. Job loop: `sb._reset()` → `main()` → emit done.
5. On idle timeout: process exits. A `_monitor` coroutine sets `alive=False`
   immediately, so the next job triggers respawn rather than being dropped.
6. On max_invocations: clean exit and respawn between jobs.
7. On crash: drop job (no requeue), respawn.

### 7.3 Queue and drop policy

- Queue full + command → busy reply (10s per-user cooldown) + drop.
- Queue full + ambient → drop silently.

### 7.4 Global process cap

`worker_max_total` in `[bot]` is a hard cap on total live workers.

---

## 8. HTTP API for external injection

The bot exposes a minimal HTTP API for injecting messages into the processing
pipeline from external tools (OBS scripts, browser extensions, local apps).

### 8.1 Configuration

```toml
[bot]
http_api_port = 8765   # 0 or omit to disable
```

```sh
# Environment (same file as other secrets)
SHIGEBOT_HTTP_SECRET=your-secret-here
```

The server binds to `127.0.0.1` only — it is never exposed to the network.

### 8.2 Endpoint

```
POST /inject HTTP/1.1
Authorization: Bearer <secret>
Content-Type: application/json
```

Request body:

```jsonc
{
    "channel":        "mychannel",  // required
    "user":           "alice",       // required — Twitch login of the sender
    "message":        "!lurk hello", // required
    "is_mod":         false,          // optional, default false
    "is_broadcaster": false           // optional, default false
}
```

Response codes:

| Code | Meaning |
|------|---------|
| 200 | `{"ok": true}` — message processed |
| 400 | `{"error": "..."}` — bad request or unknown channel |
| 401 | `{"error": "unauthorized"}` — wrong or missing secret |
| 405 | `{"error": "method not allowed"}` |

### 8.3 Behaviour

Injected messages run through the exact same pipeline as real chat messages:
ambient scripts, command dispatch, operator checks, group checks. The
`payload` (twitchio ChatMessage) is `None`, so `sb.ctx.reply` will be `None`
and `sb.reply()` falls back to an @-mention.

Built-in commands (`!refresh`, `!enable`, `!disable`, `!groups`) are skipped
for injected messages since they require a real payload to reply to.

### 8.4 Use cases

**Transcript injection (localvocal → lurk AI):**

```sh
curl -X POST http://localhost:8765/inject \
  -H "Authorization: Bearer $SHIGEBOT_HTTP_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"channel":"mychannel","user":"lolisamurai","message":"<transcript> hello chat"}'
```

**Browser extension (now playing → chat):**

```js
await fetch("http://localhost:8765/inject", {
    method: "POST",
    headers: {
        "Authorization": `Bearer ${secret}`,
        "Content-Type": "application/json",
    },
    body: JSON.stringify({
        channel: "mychannel",
        user: "yourusername",
        message: `Now playing: ${title} ${url}`,
    }),
});
```

---

## 9. Configuration

### 9.1 Bot defaults

```toml
[bot]
worker_max_invocations = 100
worker_idle_timeout    = 300
worker_max_total       = 200
worker_count           = 1
worker_queue_size      = 3
ambient_queue_size     = 0
watchdog_timeout       = 300
http_api_port          = 0       # 0 = disabled
```

### 9.2 Per-script overrides

```toml
[script_options.lurk]
worker_count = 3
queue_size   = 20

[script_options.logs]
queue_size = 100
```

### 9.3 Source aliases

```toml
[aliases]
official = "github:Francesco149/shigebot-scripts:v2/"

[scripts]
hi   = "official:hi.py"
lurk = "https://gist.github.com/..."
```

### 9.4 Operators

```toml
[bot]
operators = ["alice", "@mods", "@streamer"]

[channel_operators]
mychannel = ["bob", "-@mods"]
```

### 9.5 Event triggers

```toml
[triggers]
"stream.online"    = ["announce_live"]
"stream.offline"   = ["announce_offline"]
"channel.follow"   = ["follow"]        # moderator:read:followers scope
"channel.raid"     = ["raid"]
"channel.ad_break" = ["ad_break"]      # channel:read:ads scope
```

---

## 10. Shared channel data key conventions

| Prefix | Owner | Description |
|--------|-------|-------------|
| `bank:balance:{user}` | bank, slots, rr, fish, trivia, mirage | campbucks |
| `bank:claim:{user}` | bank | next weekly claim timestamp |
| `raids:last` | raid | login of last raider |
| `rr:*` | rr | roulette state |
| `slots:*` | slots | slots state |
| `trivia:*` | trivia | trivia state |
| `mirage:*` | mirage | mirage state |
| `fish:*` | fish | fishing state |

---

## 11. Required OAuth scopes

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

## 12. Changelog

### 2.2 (current)
- §3: Top-level imports are pre-loaded at worker startup.
- §4.1.1: Output sanitization — CRLF stripping, both `/` and `.` prefixes,
  replace with `#` (not drop), applies to plain output and action text.
- §4.3: `sb.announce()` gains optional `color` parameter.
- §4.3: New mod action helpers: `sb.shoutout()`, `sb.ban()`, `sb.timeout()`, `sb.unban()`.
- §4.4: Output delivery guarantee via `drain_event`.
- §5.3: `event_data` dict in context for trigger scripts.
- §7.2: Idle-exit fix via `_monitor` coroutine.
- §7.3: Busy reply 10-second per-user cooldown.
- §8: HTTP API for external message injection.
- §9.5: New triggers: `channel.follow`, `channel.raid`, `channel.ad_break`.
- §11: OAuth scopes table.
- Shoutout now resolves login to numeric ID before calling the API.
- Ad break subscription: `AdBreakBeginSubscription` (not `ChannelAdBreakBeginSubscription`).

### 2.1
- Persistent worker pool, `main()` entry point, action protocol,
  reply/announce/me helpers, per-script queue config, global process cap,
  reserved `kv` table.

### 2.0
- Initial v2 spec.
