# shigebot v2 script API — specification

> **This document is the single source of truth for the v2 API.**
> All implementation changes must be reflected here first.
> When asking an AI to modify the API, point it at this file.

**Spec version: 2.2**

---

## 1. Script identification

A v2 script **must** begin with the following comment on its very first line:

```python
# shigebot: v2
```

The worker manager uses this marker to detect v2 scripts and route them
through the persistent worker pool. Scripts without this marker are treated
as v1 and routed through the legacy subprocess runner unchanged.

---

## 2. Entry point

Every v2 script **must** define a `main()` function. The worker calls this
function for each invocation. Module-level code runs only once when the
worker process starts (on import); `main()` runs once per command invocation.

```python
# shigebot: v2
import shigebot as sb

def main():
    sb.say(f"hello {sb.ctx.user}")
```

**Critical rule:** never access `sb.ctx`, `sb.data`, `sb.channel`, or
`sb.global_` at module level (outside `main()`). These are populated by
`sb._reset()` immediately before each `main()` call. Module-level access
will see stale or uninitialized state.

Module-level code is appropriate for: imports, constants, helper function
definitions, class definitions.

---

## 3. Import pre-loading

Because each worker process imports the script module exactly once at
startup (before the job loop), **all top-level `import` statements execute
at worker startup**, not per-invocation. The imported modules are cached in
`sys.modules` and reused for every subsequent `main()` call.

This means:

- A script that does `import numpy as np` at the top pays the numpy import
  cost once when the worker starts, not once per `!slots` invocation.
- Module-level initialisation (loading a model, reading a config file, etc.)
  also happens once at startup.

### Preamble

The `script_preamble` in `[bot]` is exec'd in the worker process *before*
the script module is imported. Its purpose is:

1. **Cross-script pre-warming** — pre-import packages that several scripts
   share, so the first script to be loaded doesn't pay the cold-start cost.
2. **Dependency validation** — fail fast with a clear error if a required
   package is missing, rather than failing on first invocation.
3. **Global configuration** — set environment variables or modify `sys.path`
   before any script runs.

Names defined by the preamble do not leak into script namespaces. Scripts
still need their own `import` statements; the preamble just warms the cache.

Example preamble:

```toml
[bot]
script_preamble = """
# Pre-warm heavy dependencies used across multiple scripts.
import numpy
import pandas
import scipy
import requests
# Fail fast if optional AI deps are missing.
import openai
"""
```

---

## 4. Output protocol

Scripts communicate with the bot over **stdout** using a two-tier protocol.
All output must be flushed (stdout is always opened with `-u` / `PYTHONUNBUFFERED`).

### 4.1 Plain chat lines

Any line that does **not** begin with `\x00` (null byte, 0x00) is a plain
chat message and is sent to the channel verbatim.

```python
print("hello chat")   # → sent as chat message
sb.say("hello chat")  # → identical
```

Limits (enforced by the manager, not the worker):

| Limit | Value |
|-------|-------|
| Maximum lines per invocation | 10 |
| Maximum characters per line | 350 |

Lines beyond these limits are silently dropped.

### 4.2 Action lines

Lines beginning with `\x00` are JSON-encoded action descriptors. Scripts
never write these directly; they call `sb.*` helpers which emit them.

```
\x00{"action": "reply",    "to": "<msg_id>", "user": "<username>", "text": "..."}
\x00{"action": "announce", "text": "..."}
\x00{"action": "me",       "text": "..."}
\x00{"action": "done",     "job_id": "<id>"}
\x00{"action": "error",    "job_id": "<id>", "msg": "..."}
```

`done` and `error` are emitted by the **worker process**, not by scripts.
They signal to the manager that the current job has finished.

The `reply` action includes `user` so the bot can fall back to an @-mention
if the Twitch reply API is unavailable.

`\x00` will never appear as the first byte of a valid Twitch chat message,
making the sentinel unambiguous.

### 4.3 Action helpers (sb module)

```python
sb.say(text)            # plain chat message (alias for print)
sb.sayf(fmt, *args)     # print(fmt.format(*args))
sb.reply(text, to=None) # reply to triggering message (or to=msg_id)
sb.announce(text)       # channel announcement
sb.me(text)             # /me action
```

### 4.4 Output delivery guarantee

The worker pool dispatcher does **not** start the next job until all output
from the current job has been consumed and sent by the bot. This means:

- Output lines from consecutive invocations of the same command in the same
  channel are never interleaved.
- The rate limiter's backpressure propagates: if the bot is waiting to send,
  the worker is idle rather than building up a backlog of stale output.

---

## 5. Context injection

The worker manager writes a JSON job descriptor to the worker's stdin before
each invocation. The worker calls `sb._reset(ctx_blob)` before `main()`.

### 5.1 Job descriptor (stdin, one JSON line)

```jsonc
{
  "job_id":  "uuid...",
  "ctx": {
    "user":        "alice",
    "channel":     "mychannel",
    "args":        ["arg1", "arg2"],
    "msg_id":      "uuid...",
    "timestamp":   1712345678.0,
    "prefix":      "!",
    "bot_nick":    "shigebot",
    "is_ambient":  false,
    "is_operator": false,
    "script_name": "8ball",
    "channel_dir": "/var/lib/shigebot/scripts/mychannel",
    "global_dir":  "/var/lib/shigebot/scripts",
    "reply": {
      "user":       "bob",
      "message":    "original text",
      "message_id": "uuid..."
    }
  }
}
```

`reply` is `null` when the message is not a reply.

### 5.2 Context object — `sb.ctx`

```python
sb.ctx.user         # str  — Twitch login of the invoker
sb.ctx.channel      # str  — channel name (no leading #)
sb.ctx.args         # list[str]
sb.ctx.msg_id       # str  — unique message UUID
sb.ctx.timestamp    # float (Unix UTC)
sb.ctx.prefix       # str  — command prefix (e.g. "!")
sb.ctx.bot_nick     # str  — bot's Twitch login
sb.ctx.is_ambient   # bool
sb.ctx.is_operator  # bool — True if invoker is an operator for this channel
sb.ctx.script_name  # str
sb.ctx.channel_dir  # str (absolute path)
sb.ctx.global_dir   # str (absolute path)
sb.ctx.reply        # Reply | None
sb.ctx.reply.user         # str
sb.ctx.reply.message      # str
sb.ctx.reply.message_id   # str
```

### 5.3 Fallback for local testing

When `SHIGEBOT_CTX` is absent (manual terminal invocation), `shigebot.py`
builds a context from legacy env vars (`NICK`, `CHANNEL`, etc.) and
`sys.argv`. Scripts remain testable locally without the full bot stack.

---

## 6. Data stores

All stores are backed by SQLite with WAL journaling. Safe for concurrent
access from multiple worker processes hitting the same channel simultaneously.

### 6.1 Store scopes

| Store | Variable | Database file | Namespace |
|-------|----------|---------------|-----------|
| Per-script, per-channel | `sb.data` | `{channel_dir}/channel.db` | `script:{script_name}` |
| Shared channel | `sb.channel` | `{channel_dir}/channel.db` | `shared` |
| Global | `sb.global_` | `{global_dir}/global.db` | `shared` |

### 6.2 Store API

```python
store.get(key: str, default=None) -> Any
store.set(key: str, value: Any) -> None       # JSON-serialisable only
store.delete(key: str) -> None
store.all() -> dict[str, Any]
store.incr(key: str, amount=1, default=0) -> int | float  # atomic
```

### 6.3 Transactions

```python
with sb.channel.transaction() as tx:
    a = tx.get("bank:balance:alice", 0)
    b = tx.get("bank:balance:bob",   0)
    tx.set("bank:balance:alice", a - 100)
    tx.set("bank:balance:bob",   b + 100)
# committed on exit, rolled back on exception
```

### 6.4 Raw SQLite access

For complex queries or tables beyond the KV API.

```python
with sb.db() as conn:         # channel DB (sqlite3.Connection)
    conn.execute("CREATE TABLE IF NOT EXISTS fish_catalogue ...")

with sb.global_db() as conn:
    ...
```

**Reserved:** the `kv` table and any table or index prefixed with `kv_` are
owned by the shigebot runtime. Scripts must not read, write, or alter them
via raw access under any circumstances. All script-owned tables must be named
with a unique prefix (conventionally the script name, e.g. `fish_catalogue`,
`lurk_messages`, `rr_stats`).

Commits on clean context-manager exit, rolls back on exception.
Scripts are responsible for their own schema management.

### 6.5 Migration helpers

```python
# Migrate a single pickle file into a store key (runs exactly once).
sb.migrate.from_pickle(store, key, filename, transform=None) -> bool

# Load all *.pickle files in a directory.
sb.migrate.pickles_in(directory) -> dict[str, Any]
```

---

## 7. Worker model

### 7.1 Overview

For each (script, channel) pair, the worker manager maintains a **pool** of
persistent Python processes. Workers import the script once on startup and
call `main()` for each subsequent job — amortizing interpreter startup and
import costs across many invocations.

v1 scripts continue to use the legacy per-invocation subprocess runner.
v1 and v2 scripts coexist without change.

### 7.2 Worker lifecycle

1. **Spawn:** `python -u worker_process.py <script_path> <max_invocations> <idle_timeout>`
   - `PYTHONPATH` is set so `working_dir` is first, guaranteeing `import shigebot`
     finds the runtime script rather than the bot package.
   - `SHIGEBOT_PREAMBLE` is set to the preamble source if configured.
2. **Startup:** worker execs the preamble, then imports the script module (once).
3. **Job loop:** for each job read from stdin:
   a. `sb._reset(ctx_blob)` — install fresh context and store handles.
   b. Call `script.main()`.
   c. On any exception: emit `\x00{"action":"error","job_id":"...","msg":"..."}`.
   d. Always emit `\x00{"action":"done","job_id":"..."}` and flush stdout.
4. **Drain wait:** the dispatcher waits for the bot to consume and send all
   output from the completed job before pulling the next job from the queue.
   This enforces the output delivery guarantee (§4.4).
5. **Recycle:** after `max_invocations` jobs the worker exits cleanly and
   the manager spawns a replacement. Recycling happens between jobs.
6. **Idle timeout:** if no job arrives within `idle_timeout` seconds the
   worker exits. The manager spawns a fresh one on the next job.
7. **Crash:** EOF on stdout. Manager logs, optionally sends a "something went
   wrong" chat reply, spawns a replacement. Crashed job is **not** requeued
   (crash-loop protection).

### 7.3 Pool structure

```
WorkerPool(script="lurk", channel="mychannel")
  workers:     [WorkerProcess × worker_count]
  queue:       asyncio.Queue(maxsize=queue_size)
  dispatchers: [coroutine per worker, pulling from shared queue]
```

Each dispatcher pulls jobs from the shared pool queue and feeds them to its
worker one at a time. Output lines are forwarded to a per-job `result_queue`
so the bot can stream them as they arrive.

### 7.4 Queue and drop policy

When a new job arrives and `queue.full()`:

- **Command invocation:** drop the job, reply immediately:
  `"@{user} bot is busy, try again in a moment"`. Never silent.
- **Ambient invocation:** drop silently. No reply.

Workers currently executing a job are not counted against `queue.maxsize`;
`maxsize` reflects only the pending backlog.

### 7.5 Global process cap

When total live worker processes reaches `worker_max_total`, no new workers
are spawned. Jobs targeting a pool with no live workers and no spare global
slot are dropped with the busy reply (commands) or silently (ambient).

---

## 8. Configuration

### 8.1 Bot-level defaults (`[bot]` in shigebot.toml)

```toml
[bot]
worker_max_invocations = 100   # recycle after N jobs; 0 = never
worker_idle_timeout    = 300   # seconds idle before self-exit; 0 = never
worker_max_total       = 200   # hard cap on total live worker processes
worker_count           = 1     # workers per (script, channel) — default
worker_queue_size      = 3     # pending jobs cap — command scripts default
ambient_queue_size     = 0     # pending jobs cap — ambient scripts default
                               #   0 = drop immediately if all workers busy
```

### 8.2 Per-script overrides (`[script_options.<n>]`)

`queue_size` overrides `worker_queue_size` / `ambient_queue_size` for that
script regardless of ambient/command usage.

```toml
[script_options.lurk]
worker_count = 3    # parallel LLM workers per channel
queue_size   = 20   # absorbs chat spikes; drops under sustained spam

[script_options.logs]
worker_count = 1
queue_size   = 100  # very fast jobs; almost never drops

[script_options.fish]
queue_size   = 3    # slow job; small queue intentional
```

### 8.3 Source aliases (`[aliases]`)

Defines shorthand prefixes for script source URLs. An alias maps a prefix
name to a base URL. Any script URL beginning with `<alias>:` is expanded by
replacing `<alias>:` with the alias value.

```toml
[aliases]
official = "github:Francesco149/shigebot-scripts:v2/"
personal = "github:myuser/my-scripts:scripts/"
```

Usage in `[scripts]`:

```toml
[scripts]
hi      = "official:hi.py"           # → github:Francesco149/shigebot-scripts:v2/hi.py
8ball   = "official:8ball.py@v1"     # → github:Francesco149/shigebot-scripts:v2/8ball.py@v1
mytools = "personal:tools.py"        # → github:myuser/my-scripts:scripts/tools.py
lurk    = "https://gist.github.com/..." # plain URLs work unchanged
```

Aliases are expanded before any URL is fetched. Plain gist URLs and
`github:` URLs that do not match any alias prefix are used as-is.

Alias names may only contain alphanumeric characters and underscores, and
must not conflict with URL scheme prefixes (`https`, `github`).

### 8.4 Operator configuration

See `config.py` docstring for the full operator resolution rules including
per-channel overrides (`[channel_operators]`).

---

## 9. Shared channel data key conventions

Scripts writing to `sb.channel` must use these key prefixes.
New shared keys must be added to this table before use.

| Prefix | Owner scripts | Description |
|--------|---------------|-------------|
| `bank:balance:{user}` | bank, slots, rr, fish, trivia, mirage | Integer campbucks |
| `bank:claim:{user}` | bank | Unix ts of next weekly claim |
| `rr:lock:{user}` | rr | Unix ts of next daily reset |
| `rr:chamber:{user}` | rr | `{chamber, pos, won_today}` dict |
| `rr:stats:{user}` | rr | Cumulative stats dict |
| `slots:lock:{user}` | slots | Unix ts of next daily reset |
| `slots:stats:{user}` | slots | Cumulative stats dict |
| `trivia:lock:{user}` | trivia | Unix ts of next daily trivia |
| `trivia:stats:{user}` | trivia | Cumulative stats dict |
| `mirage:lock:{user}` | mirage | Unix ts of next daily mirage |
| `mirage:stats:{user}` | mirage | Cumulative stats dict |
| `fish:cooldown:{user}` | fish | Unix ts of last cast |
| `fish:items:{user}` | fish | Items inventory dict |
| `fish:dailybait_lock:{user}` | fish | Unix ts of next dailybait claim |

---

## 10. Module system

Scripts in `working_dir` are importable. Helper-only modules should not be
listed in any channel's command list.

v2 helper modules start with `# shigebot: v2`. They may use `sb.*` freely
and do not need `main()` unless also invocable as commands.

---

## 11. Design principles

- **Single source of truth:** key names in §9, context fields in §5.1,
  store methods in §6.2, action types in §4.2, config in §8.
- **Explicitness:** scripts choose `sb.data` / `sb.channel` / `sb.global_`
  explicitly.
- **Output compatibility:** output is still lines on stdout; bot send logic
  is unchanged at the Twitch API layer.
- **Testability:** fallback context means `python myscript.py arg` works
  from a terminal.
- **v1 non-interference:** v1 scripts work unchanged.

---

## 12. Changelog

### 2.2 (current)
- §3: Documented that top-level imports in v2 scripts are pre-loaded at
  worker startup (not per-invocation). Clarified preamble purpose.
- §4.4: Output delivery guarantee — dispatcher waits for full drain before
  starting next job, preventing output interleaving and rate-limiter bypass.
- §4.2: `reply` action now includes `user` field for @-mention fallback.
- §8.3: Source aliases (`[aliases]`) for shorthand URL prefixes.
- §5.1: `is_operator` field added to job context.

### 2.1
- Persistent worker pool replacing per-invocation subprocesses.
- `main()` entry point required for v2 scripts.
- `sb.ctx` is lazy — populated by `sb._reset()` before each `main()` call.
- `\x00`-prefixed JSON action protocol on stdout.
- `sb.reply()`, `sb.announce()`, `sb.me()` action helpers.
- `worker_count`, `queue_size` per-script configuration.
- `worker_max_total` global process cap.
- Reserved `kv` table and `kv_` prefix.

### 2.0
- Initial v2 spec.
