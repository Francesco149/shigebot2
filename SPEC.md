# shigebot v2 script API — specification

> **Single source of truth. All implementation changes must be reflected here first.**

**Spec version: 2.3**

---

## 1. Script identification

```python
# shigebot: v2
```

Must be the first line. Scripts without it are treated as v1.

---

## 2. Entry point

Every v2 script must define `main()`. Module-level code runs once at worker startup.

```python
# shigebot: v2
import shigebot as sb

def main():
    sb.say(f"hello {sb.ctx.user}")
```

**Never access `sb.ctx`, `sb.data`, `sb.channel`, or `sb.global_` at module level.**

---

## 3. Import pre-loading

All top-level `import` statements run once at worker startup (not per invocation).
The preamble (`script_preamble` in `[bot]`) runs even earlier, before the script
module is imported. Use it to warm shared deps across multiple scripts.

---

## 4. Output protocol

### 4.1 Plain chat lines

All output must go through `sb.say()`, `sb.sayf()`, or `print()`.

**Sanitization applied before sending (two passes):**

1. **CRLF injection** — `\n` and `\r` replaced with a space.
2. **Command prefix** — Twitch evaluates both `/` and `.` as chat-command
   prefixes. The check is applied to the left-stripped text (whitespace can't
   bypass it). Offending first character is replaced with `#`, message still
   sent, WARNING logged. Use `sb.me()`, `sb.ban()`, etc. instead.

Sanitization is also applied to text in action payloads (CRLF only).

Limits: 10 lines / 350 chars per invocation.

### 4.2 Action lines

`\x00`-prefixed JSON lines — only via `sb.*` helpers, never directly.

```
\x00{"action": "reply",    "to": "<msg_id>", "user": "<login>", "text": "..."}
\x00{"action": "announce", "text": "...", "color": "blue"}
\x00{"action": "me",       "text": "..."}
\x00{"action": "shoutout", "target": "<login>"}
\x00{"action": "ban",      "target": "<login>", "reason": "..."}
\x00{"action": "timeout",  "target": "<login>", "duration": 600, "reason": "..."}
\x00{"action": "unban",    "target": "<login>"}
\x00{"action": "done",     "job_id": "<id>"}   # worker only
\x00{"action": "error",    "job_id": "<id>", "msg": "..."}  # worker only
```

### 4.3 Output helpers

```python
sb.say(text)
sb.sayf(fmt, *args)
sb.reply(text, to=None)
sb.announce(text, color=None)      # color: blue|green|orange|purple|primary
sb.me(text)
sb.shoutout(target)                # resolves login → numeric ID before API call
sb.ban(target, reason="")
sb.timeout(target, duration=600, reason="")
sb.unban(target)
```

### 4.4 Output delivery guarantee

Dispatcher awaits `drain_event` before pulling the next job — no interleaving,
no stale output building up behind the rate limiter.

---

## 5. Context

### 5.1 Job descriptor schema

```jsonc
{
  "job_id": "uuid...",
  "ctx": {
    "user": "alice", "channel": "mychannel", "args": ["arg1"],
    "msg_id": "uuid...", "timestamp": 1712345678.0,
    "prefix": "!", "bot_nick": "shigebot",
    "is_ambient": false, "is_operator": false,
    "script_name": "8ball",
    "channel_dir": "/var/lib/shigebot/scripts/mychannel",
    "global_dir":  "/var/lib/shigebot/scripts",
    "event_data":  {},
    "reply": {"user": "bob", "message": "...", "message_id": "uuid..."} | null
  }
}
```

### 5.2 sb.ctx fields

```python
sb.ctx.user / channel / args / msg_id / timestamp
sb.ctx.prefix / bot_nick / is_ambient / is_operator / script_name
sb.ctx.channel_dir / global_dir   # absolute path strings
sb.ctx.event_data                 # dict — see §5.3
sb.ctx.reply                      # Reply | None
sb.ctx.reply.user / message / message_id
```

### 5.3 event_data for trigger scripts

| Event | Keys |
|-------|------|
| `stream.online` | `stream_type: str` |
| `stream.offline` | _(empty)_ |
| `channel.follow` | `from_user`, `from_user_display` |
| `channel.raid` | `from_user`, `from_user_display`, `viewer_count: int` |
| `channel.ad_break` | `duration: int` (seconds), `is_automatic: bool` |

---

## 6. Data stores

### 6.1 Scopes

| Variable | DB file | Namespace |
|----------|---------|-----------|
| `sb.data` | `{channel_dir}/channel.db` | `script:{script_name}` |
| `sb.channel` | `{channel_dir}/channel.db` | `shared` |
| `sb.global_` | `{global_dir}/global.db` | `shared` |

### 6.2 API

```python
store.get(key, default=None) / set(key, value) / delete(key) / all() / incr(key, amount, default)
```

Transactions: `with store.transaction() as tx: tx.get/set/delete/incr(...)`

Raw access: `with sb.db() / sb.global_db() as conn: ...`

**Reserved:** `kv` table and `kv_`-prefixed tables.

---

## 7. Worker model

### 7.1 Lifecycle

1. Spawn → `PYTHONPATH` prepended with `working_dir` → import preamble → import script.
2. Job loop: `_reset()` → `main()` → `done`.
3. **Idle exit**: `_monitor()` coroutine sets `alive=False` immediately on exit,
   so the next job triggers respawn rather than being dropped.
4. **Script update**: when `fetch_one()` returns True (file changed),
   `worker_manager.invalidate_script(name)` stops all pools for that script.
   The next invocation creates a fresh pool with a fresh worker that imports
   the new code. Both auto-refresh and `!refresh` call this.
5. Busy reply: 10s per-user cooldown to avoid flooding the rate limiter.

---

## 8. HTTP inject API

### 8.1 Config

```toml
[bot]
http_api_port = 8765          # 0 = disabled
http_api_host = "127.0.0.1"   # loopback (default)
# http_api_host = "0.0.0.0"   # all interfaces, expose on LAN
```

```sh
SHIGEBOT_HTTP_SECRET=your-secret
```

**Troubleshooting**: If you receive an S3/XML error response, another process
(e.g. garage, minio) is listening on that port — change `http_api_port`.

### 8.2 Endpoint

```
POST /inject
Authorization: Bearer <secret>
Content-Type: application/json

{"channel":"mychannel","user":"alice","message":"!echo hello","is_mod":false,"is_broadcaster":false}
```

### 8.3 Behaviour

Full ambient + command pipeline. Built-in commands (`!refresh`, `!enable`,
`!disable`, `!groups`) are skipped for injected messages.

---

## 9. Browser mode (Pyodide/WASM)

The browser distribution (`browser/index.html` + `browser/shigebot_browser.py`)
runs shigebot entirely in the browser via Pyodide (Python compiled to WASM).

### 9.1 Architecture differences from the server version

| Feature | Server | Browser |
|---------|--------|---------|
| Twitch connection | twitchio (EventSub WebSocket) | js.WebSocket (native) |
| Script execution | subprocess workers | importlib in-process |
| State storage | SQLite files on disk | localStorage |
| OAuth | CLI wizard (`shigebot-auth`) | PKCE browser flow |
| HTTP inject API | Yes | **Disabled** |
| v1 scripts | Yes | **Disabled** |
| Process isolation | Yes | **No** — scripts share the Python VM |

### 9.2 Script execution in browser mode

Scripts are loaded from localStorage (fetched from gist/github on demand) and
executed in-process via `importlib`. `import shigebot as sb` resolves to a
lightweight shim (`_BrowserSb`) that buffers `sb.say()` output and action calls,
returning them after `main()` returns.

### 9.3 Build

```sh
# Serve locally (Pyodide loads from CDN — requires internet on first load)
nix run .#browser-serve

# Or build and serve manually
nix build .#browser
cd result && python3 -m http.server 8080
```

See `browser/flake_additions.nix` for the derivation to merge into `flake.nix`.

---

## 10. Configuration reference

```toml
[bot]
nick                      = "shigebot"
bot_id                    = "123456789"
prefix                    = "!"
operators                 = ["alice", "@mods", "@streamer"]
working_dir               = "/var/lib/shigebot/scripts"
gist_refresh_interval     = 300
script_timeout            = 10
script_preamble           = ""
refresh_user_limit        = 10
refresh_user_window       = 60.0
rate_limit_window         = 30.0
rate_limit_non_elevated_max = 18
rate_limit_elevated_max   = 95
worker_max_invocations    = 100
worker_idle_timeout       = 300
worker_max_total          = 200
worker_count              = 1
worker_queue_size         = 3
ambient_queue_size        = 0
watchdog_timeout          = 300
http_api_port             = 0       # 0 = disabled
http_api_host             = "127.0.0.1"

[aliases]
official = "github:Francesco149/shigebot-scripts:v2/"

[channel_operators]
mychannel = ["bob", "-@mods"]

[groups]
games = ["slots", "rr", "fish"]

[triggers]
"stream.online"    = ["announce_live"]
"stream.offline"   = ["announce_offline"]
"channel.follow"   = ["follow"]
"channel.raid"     = ["raid"]
"channel.ad_break" = ["ad_break"]

[channels]
mychannel = ["@all", "#lurk", "#logs", "-ratelimit"]

[scripts]
hi = "official:hi.py"

[script_options.lurk]
worker_count = 3
queue_size   = 20
```

---

## 11. Shared channel data keys

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

## 12. Required OAuth scopes

| Scope | Required for |
|-------|-------------|
| `user:read:chat` / `user:write:chat` / `user:bot` | Chat |
| `moderator:manage:announcements` | `sb.announce()` |
| `moderator:read:followers` | `channel.follow` trigger |
| `channel:read:ads` | `channel.ad_break` trigger |
| `moderator:manage:shoutouts` | `sb.shoutout()` |
| `moderator:manage:banned_users` | `sb.ban()`, `sb.timeout()`, `sb.unban()` |

---

## 13. Changelog

### 2.3 (current)
- §7.1: Worker restart on script change (`invalidate_script`). Both auto-refresh
  and `!refresh` now restart affected workers when the script file changes on disk.
- §8.1: `http_api_host` config field — expose inject API on LAN with `"0.0.0.0"`.
  Added troubleshooting note for S3/XML port-conflict error.
- §9: Browser mode (Pyodide/WASM) documented with architecture comparison table.
- Shoutout resolves login → numeric user ID before calling Twitch API.
- `reply_to_message_id` kwarg (not `reply_parent_message_id`) confirmed.

### 2.2
- Output sanitization (CRLF, `/` and `.` prefix), `drain_event`, event hooks
  (follow/raid/ad break), mod action helpers, source aliases.

### 2.1
- Persistent worker pool, `main()` entry point, action protocol, per-script
  queue config, global process cap, reserved `kv` table.

### 2.0
- Initial v2 spec.
