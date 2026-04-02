"""
shigebot/worker_process.py — persistent worker subprocess for v2 scripts.

Spawned once per (script, channel) pair by the worker manager. Imports the
script module once, then handles many jobs by calling main() in a loop.

Usage (internal — do not invoke directly):
    python -u worker_process.py <script_path> <max_invocations> <idle_timeout>

Environment (set by worker manager):
    PYTHONPATH          — must include the scripts working_dir so that
                          `import shigebot` finds the runtime script rather
                          than the shigebot bot package
    SHIGEBOT_PREAMBLE   — optional Python source exec'd once at worker startup,
                          before the script module is imported; used to
                          pre-cache heavyweight dependencies and validate that
                          required packages are installed

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

_ACTION    = "\x00"
_IDLE_POLL = 1.0   # seconds between stdin-ready checks when idle


def _emit(obj: dict) -> None:
    print(_ACTION + json.dumps(obj), flush=True)


def _emit_done(job_id: str) -> None:
    _emit({"action": "done", "job_id": job_id})


def _emit_error(job_id: str, msg: str) -> None:
    _emit({"action": "error", "job_id": job_id, "msg": msg})


# ── sys.path bootstrap ────────────────────────────────────────────────────

def _bootstrap_syspath() -> None:
    """
    Ensure the scripts working_dir is at the front of sys.path so that
    `import shigebot` resolves to the runtime script (shigebot.py in
    working_dir) rather than the shigebot bot package in site-packages.

    The worker is always spawned with cwd=working_dir, so os.getcwd() is
    reliable here. We also honour any PYTHONPATH entries that were injected
    by the worker manager.
    """
    working_dir = os.getcwd()

    # Remove working_dir from wherever it might already sit so we can
    # re-insert it at position 0 (highest priority).
    while working_dir in sys.path:
        sys.path.remove(working_dir)
    sys.path.insert(0, working_dir)

    # Also ensure PYTHONPATH entries are present (they may not be if
    # Python was launched in a way that skips PYTHONPATH processing).
    pythonpath = os.environ.get("PYTHONPATH", "")
    for entry in reversed(pythonpath.split(os.pathsep)):
        entry = entry.strip()
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)

    # If shigebot was already imported (shouldn't happen in a fresh exec,
    # but be defensive), evict it so the next import re-resolves from the
    # corrected sys.path.
    sys.modules.pop("shigebot", None)


# ── Preamble execution ────────────────────────────────────────────────────

def _exec_preamble() -> None:
    """
    Execute SHIGEBOT_PREAMBLE in the worker's global namespace.

    The preamble is typically used to:
      - Pre-import heavy dependencies (numpy, pandas, …) so they are
        cached in sys.modules before any script imports them
      - Fail fast with a clear error if a required package is missing

    Names defined by the preamble are not injected into script modules.
    Scripts must still use their own `import` statements; the preamble just
    ensures the packages are installed and warms the import cache.
    """
    preamble = os.environ.get("SHIGEBOT_PREAMBLE", "").strip()
    if not preamble:
        return
    try:
        # exec in a fresh namespace; side-effects (sys.modules entries) persist
        exec(compile(preamble, "<preamble>", "exec"), {})  # noqa: S102
        print("[worker] preamble executed ok", file=sys.stderr, flush=True)
    except Exception as exc:
        print(
            f"[worker] preamble failed: {exc}\n{traceback.format_exc()}",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)


# ── Script loader ─────────────────────────────────────────────────────────

def _load_script(script_path: str) -> types.ModuleType:
    """
    Import the script at `script_path` as a freshly-loaded module.
    The module name is derived from the file stem.
    Module-level code runs exactly once here.
    """
    p    = os.path.abspath(script_path)
    stem = os.path.splitext(os.path.basename(p))[0]

    spec = importlib.util.spec_from_file_location(stem, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load script: {p!r}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[stem] = module       # make it importable by peer scripts
    spec.loader.exec_module(module)  # runs module-level code once
    return module


# ── Job reader ────────────────────────────────────────────────────────────

def _read_jobs(idle_timeout: float):
    """
    Generator that yields parsed job dicts from stdin.

    Exits cleanly when:
      - stdin EOF (manager process died)
      - idle_timeout > 0 and no job arrives within that many seconds
    """
    import select

    start_idle = time.monotonic()

    while True:
        if idle_timeout > 0:
            ready, _, _ = select.select([sys.stdin], [], [], _IDLE_POLL)
            if not ready:
                elapsed = time.monotonic() - start_idle
                if elapsed >= idle_timeout:
                    print(
                        f"[worker] idle timeout after {elapsed:.0f}s — exiting",
                        file=sys.stderr, flush=True,
                    )
                    return
                continue
        else:
            ready = [sys.stdin]  # no timeout: block

        line = sys.stdin.readline()
        if not line:
            return  # stdin closed

        start_idle = time.monotonic()
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[worker] bad job JSON: {exc}", file=sys.stderr, flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────

def run(script_path: str, max_invocations: int, idle_timeout: float) -> None:
    # 1. Fix sys.path so working_dir/shigebot.py wins over the bot package.
    _bootstrap_syspath()

    # 2. Run the preamble (dependency pre-caching / validation).
    _exec_preamble()

    # 3. Import the shigebot v2 runtime — now guaranteed to find shigebot.py.
    try:
        import shigebot as sb
    except ImportError as exc:
        print(f"[worker] cannot import shigebot runtime: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    # 4. Import the script module once — module-level code runs here.
    try:
        script = _load_script(script_path)
    except Exception as exc:
        print(
            f"[worker] failed to load {script_path!r}: {exc}\n{traceback.format_exc()}",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)

    if not hasattr(script, "main"):
        print(
            f"[worker] {script_path!r} has no main() — not a valid v2 script",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)

    print(f"[worker] ready ({script_path})", file=sys.stderr, flush=True)

    # 5. Job loop.
    invocations = 0

    for job in _read_jobs(idle_timeout):
        job_id  = job.get("job_id", "unknown")
        ctx_raw = job.get("ctx", {})

        try:
            sb._reset(ctx_raw)
        except Exception as exc:
            _emit_error(job_id, f"context reset failed: {exc}")
            _emit_done(job_id)
            continue

        try:
            script.main()
        except SystemExit:
            pass  # scripts may call sys.exit() — treat as clean finish
        except Exception:
            tb = traceback.format_exc()
            print(f"[worker] exception in {script_path!r}:\n{tb}", file=sys.stderr, flush=True)
            _emit_error(job_id, traceback.format_exc(limit=3).strip())
        finally:
            sys.stdout.flush()
            _emit_done(job_id)

        invocations += 1
        if max_invocations > 0 and invocations >= max_invocations:
            print(
                f"[worker] max_invocations ({max_invocations}) reached — recycling",
                file=sys.stderr, flush=True,
            )
            return


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <script_path> <max_invocations> <idle_timeout>",
            file=sys.stderr,
        )
        sys.exit(1)

    run(
        script_path     = sys.argv[1],
        max_invocations = int(sys.argv[2]),
        idle_timeout    = float(sys.argv[3]),
    )
