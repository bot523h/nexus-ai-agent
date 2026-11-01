# MISSION 3: Single Queue Owner Enforcement (P1)

**Status:** DEFERRED (blocked by task-256 merged + lease released)
**Owner:** arena/1f7a74db-nexus-ai-agent
**Strengthens:** task-263 (startup recovery)

## Problem

The startup recovery mechanism (`resume_pending`) relies on a single process
owning the SQLite sidecar. This is an architectural assumption, not an enforced
invariant. The docstring for `resume_pending` states:

> "relies on the composition roots' exclusive queue-owning process per sidecar:
> the previous owner is dead by construction"

But nothing in the code enforces this. If two processes start simultaneously
(or an old worker respawns), both can claim in-flight rows, leading to:

1. **Double execution** — two workers run the same job
2. **Lost fencing** — a stale worker's completion can overwrite a newer worker's
3. **Causal corruption** — the journal records events from both workers

## Current State (Read-Only Audit)

### What exists

- `InProcessJobQueue` uses SQLite for durability
- `_db_lock = threading.Lock()` — cross-thread lock within one process
- `resume_pending()` — startup recovery that resets in-flight rows
- `_reset_unfinished()` — SQL-level reset of unfinished rows
- Fencing tokens — monotonically increasing per reservation
- CAS (Compare-And-Swap) — `expected_attempt` parameter on cancellation

### What does NOT exist

- No OS-level file lock on the sidecar (`fcntl`, `flock`, `os.lockf`)
- No `O_EXCL` / `O_CREAT` atomic file creation for ownership
- No PID file as proof of ownership (and none should be used as sole proof)
- No wall-clock expiration as a substitute for ownership (and none should be)
- No cross-process lock to prevent double-start

### Key code locations

| Location | What |
|---|---|
| `adapters/in_process_job_queue.py:313` | `_db_lock = threading.Lock()` (in-process only) |
| `adapters/in_process_job_queue.py:717` | `resume_pending()` — startup recovery |
| `adapters/in_process_job_queue.py:726` | Docstring: "relies on the composition roots' exclusive queue-owning process per sidecar" |
| `bot/app.py:238` | Composition root: `InProcessJobQueue(_queue_db, ...)` — no lock parameter |

## Design

### Exclusive Sidecar Ownership

Implement real exclusive ownership per file-backed queue sidecar using
OS-managed file locking:

```
<sidecar_path>.lock  ←  flock(LOCK_EX | LOCK_NB) on open file descriptor
```

The lock file is opened with `O_CREAT | O_RDWR`, then `fcntl.flock(fd, LOCK_EX | LOCK_NB)`
is called. If the lock is already held by another process, `BlockingIOError`
is raised — the second process refuses to start.

### Key properties

1. **OS-managed** — `flock` is released automatically when the process dies
   (the file descriptor is closed by the OS). No cleanup needed.
2. **Not a PID file** — the lock is on an open file descriptor, not a PID
   file. A PID file is a hint; a file lock is an invariant.
3. **Not wall-clock expiration** — the lock has no time-based expiration.
   The owner either holds it or doesn't.
4. **Acquired at startup** — before any DB operations, the queue acquires
   the exclusive lock. If it fails, the process exits with a clear error.
5. **Released at shutdown** — on clean shutdown, the lock is explicitly
   released. On crash, the OS releases it.

### Implementation plan

#### Phase 1: Lock acquisition

```python
def _acquire_sidecar_lock(sidecar_path: Path) -> int:
    """Acquire exclusive flock on the sidecar. Returns the file descriptor.
    Raises BlockingIOError if already locked by another process."""
    lock_path = Path(f"{sidecar_path}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(f"queue sidecar already locked by another process: {lock_path}")
    return fd
```

#### Phase 2: Lock integration

- `InProcessJobQueue.__init__` acquires the lock (or accepts a `lock_fd` parameter)
- `InProcessJobQueue.close()` releases the lock
- The lock is held for the lifetime of the queue object

#### Phase 3: Startup integration

- `bot/app.py` composition root: the queue is created with the lock
- If the lock fails, the bot exits with a clear error
- CLI entry points (`nexus jobs resume`, etc.) also acquire the lock

#### Phase 4: Tests

### Mandatory tests

| # | Test | What it proves |
|---|---|---|
| 1 | Two processes, one sidecar | First holds lock, second refuses |
| 2 | First terminates, successor acquires | Lock released on process death |
| 3 | Same-process double-owner | Second queue instance in same process is refused |
| 4 | Lock permissions | Lock file has restrictive permissions (0o600) |
| 5 | Lock symlink verification | Lock file is a regular file, not a symlink |
| 6 | Subprocess crash | OS releases lock when subprocess crashes |
| 7 | Subprocess double-start | Two subprocesses cannot both acquire |

### Subprocess tests

Tests 1, 2, 6, 7 require subprocess isolation:
- Spawn a Python subprocess that acquires the lock
- Verify the parent cannot acquire
- Kill the subprocess
- Verify the parent can now acquire

### What is NOT a substitute

| Approach | Why it's insufficient |
|---|---|
| PID file | A hint, not an invariant. Can be stale, forged, or raced. |
| Wall-clock expiration | A guess, not a fact. Can allow double-execution during the window. |
| SQLite WAL mode | WAL allows concurrent readers but not exclusive writer ownership across processes. |
| SQLite `BEGIN EXCLUSIVE` | Only prevents concurrent writes, not concurrent queue ownership. Two processes can both own the queue and interleave their writes. |
| Advisory locks in SQLite | Not available in Python's sqlite3 module. |

## Dependencies

- **Blocked by:** task-256 merged + lease released (task-256 touches `in_process_job_queue.py`)
- **Strengthens:** task-263 (startup recovery)
- **Coordinates with:** MISSION 1 (effect fencing), MISSION 2 (publication hardening)

## Evidence

- Read-only audit of `in_process_job_queue.py`:
  - Line 313: `_db_lock = threading.Lock()` — in-process only
  - Line 717-760: `resume_pending()` — relies on architectural assumption
  - Line 726: Docstring confirms "relies on the composition roots' exclusive queue-owning process per sidecar"
- No `fcntl`, `flock`, `os.lockf`, `O_EXCL`, `O_CREAT` in the queue code
- No lock file creation in `bot/app.py` composition root
