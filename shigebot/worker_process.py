"""
shigebot/worker_process.py — persistent worker subprocess for v2 scripts.

Spawned once per (script, channel) pair by the worker manager. Imports the
script module once, then handles many jobs by calling main() in a loop.

Usage (internal — do not invoke directly):
    python -u worker_process.py <script_path> <max_invocations> <idle_timeout>

stdin:  one JSON line per job → {"job_id": "...", "ctx": {...}}
stdout: zero or more output lines per job, either:
          - plain UTF-8 line (chat message, no \x00 prefix)
          - \x00-prefixed JSON action line
        always terminated by \x00{"action":"done","job_id":"..."}
        or     \x00{"action":"error","job_id":"...","msg":"..."}
               followed by \x00{"action":"done","job_id":"..."}
stderr: debug/error logging (captured by manager, logged only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback
import types

# ── Constants ─────────────────────────────────────────────────────────────

_ACTION = "\x00"
_IDLE_POLL = 1.0   # seconds between stdin-ready checks when idle


def _emit(obj: dict) -> None:
    print(_ACTION + json.dumps(obj), flush=True)


def _emit_done(job_id: str) -> None:
    _emit({"action": "done", "job_id": job_id})


def _emit_error(job_id: str, msg: str) -> None:
    _emit({"action": "error", "job_id": job_id, "msg": msg})


# ── Script loader ─────────────────────────────────────────────────────────

def _load_script(script_path: str) -> types.ModuleType:
    """
    Import the script at `script_path` as a module.
    The module name is derived from the file stem.
    """
    p = os.path.abspath(script_path)
    stem = os.path.splitext(os.path.basename(p))[0]

    # Ensure the script's directory is on sys.path so relative imports work.
    script_dir = os.path.dirname(p)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    spec = importlib.util.spec_from_file_location(stem, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load script: {p!r}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module          # make it importable by other scripts
    spec.loader.exec_module(module)     # runs module-level code once
    return module


# ── Job reader ────────────────────────────────────────────────────────────

def _read_jobs(idle_timeout: float):
    """
    Generator that yields parsed job dicts from stdin.
    Exits cleanly (StopIteration) when:
      - stdin closes (bot process died)
      - idle_timeout > 0 and no job arrives within that many seconds
    """
    import select

    start_idle = time.monotonic()

    while True:
        # Non-blocking check so we can implement idle timeout portably.
        if idle_timeout > 0:
            ready, _, _ = select.select([sys.stdin], [], [], _IDLE_POLL)
            if not ready:
                elapsed = time.monotonic() - start_idle
                if elapsed >= idle_timeout:
                    print(f"[worker] idle timeout after {elapsed:.0f}s — exiting",
                          file=sys.stderr, flush=True)
                    return
                continue
        else:
            # No timeout: block until data available.
            ready = [sys.stdin]

        line = sys.stdin.readline()
        if not line:
            # stdin closed — manager is gone.
            return

        start_idle = time.monotonic()
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[worker] bad job JSON: {exc}", file=sys.stderr, flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────

def run(script_path: str, max_invocations: int, idle_timeout: float) -> None:
    # Import shigebot runtime (must be on sys.path already via PYTHONPATH).
    try:
        import shigebot as sb
    except ImportError as exc:
        print(f"[worker] cannot import shigebot: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    # Import the script module once — module-level code runs here.
    try:
        script = _load_script(script_path)
    except Exception as exc:
        print(f"[worker] failed to load {script_path!r}: {exc}",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    if not hasattr(script, "main"):
        print(f"[worker] {script_path!r} has no main() — not a valid v2 script",
              file=sys.stderr, flush=True)
        sys.exit(1)

    print(f"[worker] ready ({script_path})", file=sys.stderr, flush=True)

    invocations = 0

    for job in _read_jobs(idle_timeout):
        job_id  = job.get("job_id", "unknown")
        ctx_raw = job.get("ctx", {})

        # Install fresh context and store handles for this invocation.
        try:
            sb._reset(ctx_raw)
        except Exception as exc:
            _emit_error(job_id, f"context reset failed: {exc}")
            _emit_done(job_id)
            continue

        # Run the script.
        try:
            script.main()
        except SystemExit:
            pass        # scripts may call sys.exit() — treat as clean exit
        except Exception:
            tb = traceback.format_exc()
            print(f"[worker] exception in {script_path!r}:\n{tb}",
                  file=sys.stderr, flush=True)
            _emit_error(job_id, traceback.format_exc(limit=3).strip())
        finally:
            sys.stdout.flush()
            _emit_done(job_id)

        invocations += 1
        if max_invocations > 0 and invocations >= max_invocations:
            print(f"[worker] reached max_invocations ({max_invocations}) — recycling",
                  file=sys.stderr, flush=True)
            return


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <script_path> <max_invocations> <idle_timeout>",
              file=sys.stderr)
        sys.exit(1)

    _script_path      = sys.argv[1]
    _max_invocations  = int(sys.argv[2])
    _idle_timeout     = float(sys.argv[3])

    run(_script_path, _max_invocations, _idle_timeout)
