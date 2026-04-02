# shigebot v2 script API — specification

> **This document is the single source of truth for the v2 API.**
> All implementation changes must be reflected here first.
> When asking an AI to modify the API, point it at this file.

**Spec version: 2.1**

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

## 3. Output protocol

Scripts communicate with the bot over **stdout** using a two-tier protocol.
All output must be flushed (stdout is always opened with `-u` / `PYTHONUNBUFFERED`).

### 3.1 Plain chat lines

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

### 3.2 Action lines

Lines beginning with `\x00` are JSON-encoded action descriptors. Scripts
never write these directly; they call `sb.*` helpers which emit them.

```
\x00{"action": "reply",    "to": "<msg_id>", "text": "..."}
\x00{"action": "announce", "text": "..."}
\x00{"action": "me",       "text": "..."}
\x00{"action": "done",     "job_id": "<id>"}
\x00{"action": "error",    "job_id": "<id>", "msg": "..."}
```

`done` and `error` are emitted by the **worker process**, not by scripts.
They signal to the manager that the current job has finished.

`\x00` will never appear as the first byte of a valid Twitch chat message,
making the sentinel unambiguous.

### 3.3 Action helpers (sb module)

```python
sb.say(text)            # plain chat message (alias for print)
sb.sayf(fmt, *args)     # print(fmt.format(*args))
sb.reply(text, to=None) # reply to triggering message (or to=msg_id)
sb.announce(text)       # channel announcement
sb.me(text)             # /me action
```

---

## 4. Context injection

The worker manager writes a JSON job descriptor to the worker's stdin before
each invocation. The worker calls `sb._reset(ctx_blob)` before `main()`.

### 4.1 Job descriptor (stdin, one JSON line)

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

### 4.2 Context object — `sb.ctx`

```python
sb.ctx.user         # str
sb.ctx.channel      # str
sb.ctx.args         # list[str]
sb.ctx.msg_id       # str
sb.ctx.timestamp    # float (Unix UTC)
sb.ctx.prefix       # str
sb.ctx.bot_nick     # str
sb.ctx.is_ambient   # bool
sb.ctx.script_name  # str
sb.ctx.channel_dir  # str (absolute path)
sb.ctx.global_dir   # str (absolute path)
sb.ctx.reply        # Reply | None
sb.ctx.reply.user         # str
sb.ctx.reply.message      # str
sb.ctx.reply.message_id   # str
```

### 4.3 Fallback for local testing

When `SHIGEBOT_CTX` is absent (manual terminal invocation), `shigebot.py`
builds a context from legacy env vars (`NICK`, `CHANNEL`, etc.) and
`sys.argv`. Scripts remain testable locally without the full bot stack.

In this mode the worker loop is bypassed: the module calls `main()` directly
when run as `__main__` if the script defines it.

---

## 5. Data stores

All stores are backed by SQLite with WAL journaling. Safe for concurrent
access from multiple worker processes hitting the same channel simultaneously.

### 5.1 Store scopes

| Store | Variable | Database file | Namespace |
|-------|----------|---------------|-----------|
| Per-script, per-channel | `sb.data` | `{channel_dir}/channel.db` | `script:{script_name}` |
| Shared channel | `sb.channel` | `{channel_dir}/channel.db` | `shared` |
| Global | `sb.global_` | `{global_dir}/global.db` | `shared` |

### 5.2 Store API

```python
store.get(key: str, default=None) -> Any
store.set(key: str, value: Any) -> None       # JSON-serialisable only
store.delete(key: str) -> None
store.all() -> dict[str, Any]
store.incr(key: str, amount=1, default=0) -> int | float  # atomic
```

### 5.3 Transactions

```python
with sb.channel.transaction() as tx:
    a = tx.get("bank:balance:alice", 0)
    b = tx.get("bank:balance:bob",   0)
    tx.set("bank:balance:alice", a - 100)
    tx.set("bank:balance:bob",   b + 100)
# committed on exit, rolled back on exception
```

### 5.4 Raw SQLite access

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

### 5.5 Migration helpers

```python
# Migrate a single pickle file into a store key (runs exactly once).
sb.migrate.from_pickle(store, key, filename, transform=None) -> bool

# Load all *.pickle files in a directory.
sb.migrate.pickles_in(directory) -> dict[str, Any]
```

---

## 6. Worker model

### 6.1 Overview

For each (script, channel) pair, the worker manager maintains a **pool** of
persistent Python processes. Workers import the script once on startup and
call `main()` for each subsequent job — amortizing interpreter startup and
import costs across many invocations.

v1 scripts continue to use the legacy per-invocation subprocess runner.
v1 and v2 scripts coexist without change.

### 6.2 Worker lifecycle

1. **Spawn:** `python -u worker_process.py <script_path> <max_invocations> <idle_timeout>`
2. **Import:** worker imports `shigebot` and the script module once.
3. **Job loop:** for each job read from stdin:
   a. `sb._reset(ctx_blob)` — install fresh context and store handles.
   b. Call `script.main()`.
   c. On any exception: emit `\x00{"action":"error","job_id":"...","msg":"..."}`.
   d. Always emit `\x00{"action":"done","job_id":"..."}` and flush stdout.
4. **Recycle:** after `max_invocations` jobs the worker emits done, exits
   cleanly. The manager spawns a replacement. Recycling happens between jobs.
5. **Idle timeout:** if no job arrives within `idle_timeout` seconds the
   worker exits. The manager spawns a fresh one on the next job.
6. **Crash:** EOF on stdout. Manager logs, optionally sends a "something went
   wrong" chat reply, spawns a replacement. Crashed job is **not** requeued
   (crash-loop protection).

### 6.3 Pool structure

```
WorkerPool(script="lurk", channel="mychannel")
  workers:     [WorkerProcess × worker_count]
  queue:       asyncio.Queue(maxsize=queue_size)
  dispatchers: [coroutine per worker, pulling from shared queue]
```

Each dispatcher pulls jobs from the shared pool queue and feeds them to its
worker one at a time. Output lines are forwarded to a per-job `result_queue`
so the bot can stream them as they arrive.

### 6.4 Queue and drop policy

When a new job arrives and `queue.full()`:

- **Command invocation:** drop the job, reply immediately:
  `"@{user} bot is busy, try again in a moment"`. Never silent.
- **Ambient invocation:** drop silently. No reply.

Workers currently executing a job are not counted against `queue.maxsize`;
`maxsize` reflects only the pending backlog.

### 6.5 Global process cap

When total live worker processes reaches `worker_max_total`, no new workers
are spawned. Jobs targeting a pool with no live workers and no spare global
slot are dropped with the busy reply (commands) or silently (ambient).

---

## 7. Configuration

### 7.1 Bot-level defaults (`[bot]` in shigebot.toml)

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

### 7.2 Per-script overrides (`[script_options.<name>]`)

`queue_size` overrides both `worker_queue_size` and `ambient_queue_size` for
that script regardless of ambient/command usage.

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

The existing `[scripts]` table is unchanged:

```toml
[scripts]
lurk = "https://gist.github.com/..."
logs = "https://gist.github.com/..."
```

---

## 8. Shared channel data key conventions

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

## 9. Module system

Scripts in `working_dir` are importable. Helper-only modules should not be
listed in any channel's command list.

v2 helper modules start with `# shigebot: v2`. They may use `sb.*` freely
and do not need `main()` unless also invocable as commands.

---

## 10. Design principles

- **Single source of truth:** key names in §8, context fields in §4.1,
  store methods in §5.2, action types in §3.2, config in §7.
- **Explicitness:** scripts choose `sb.data` / `sb.channel` / `sb.global_`
  explicitly.
- **Output compatibility:** output is still lines on stdout; bot send logic
  is unchanged at the Twitch API layer.
- **Testability:** fallback context means `python myscript.py arg` works
  from a terminal.
- **v1 non-interference:** v1 scripts work unchanged.

---

## 11. Changelog

### 2.1 (current)
- Persistent worker pool replacing per-invocation subprocesses.
- `main()` entry point required for v2 scripts.
- `sb.ctx` is lazy — populated by `sb._reset()` before each `main()` call.
  Module-level access is a spec violation.
- `\x00`-prefixed JSON action protocol on stdout.
- `sb.reply()`, `sb.announce()`, `sb.me()` action helpers.
- `worker_count`, `queue_size` per-script configuration via `[script_options]`.
- `worker_max_total` global process cap.
- Reserved `kv` table and `kv_` prefix.

### 2.0
- Initial v2 spec.
