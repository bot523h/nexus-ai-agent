"""task-283 — queue ownership across real processes.

These tests use real OS processes (no mocks, no monkeypatching) because the
invariants are about the *kernel*, not about Python state:

* at most one live process may own a sidecar,
* a killed owner's lock disappears with it,
* a contender that loses never starts recovery,
* simultaneous contenders on an abandoned lock produce exactly one winner —
  the race the previous PID lock-file implementation lost.

Each subprocess is given ``PYTHONPATH=src`` and the repository root as cwd, so
it imports the same tree under test.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"}


def _spawn(script: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run(script: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_HOLDER_TEMPLATE = """
import asyncio, os
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
q = InProcessJobQueue({db!r})
asyncio.run(q.acquire_ownership())
print('READY', flush=True)
asyncio.run(asyncio.sleep({sleep}))
"""


def _wait_ready(proc: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if line.strip() == "READY":
            return
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"holder exited early: {stderr}")
    raise AssertionError("holder did not become ready")


# ── exclusivity ──────────────────────────────────────────────────────────────


def test_second_live_process_is_refused(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.sqlite3")
    holder = _spawn(_HOLDER_TEMPLATE.format(db=db, sleep=60))
    try:
        _wait_ready(holder)
        assert holder.poll() is None, "the holder must be alive while exclusion is tested"

        loser = _run(
            "import asyncio\n"
            "from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue\n"
            f"q = InProcessJobQueue({db!r})\n"
            "asyncio.run(q.acquire_ownership())\n"
            "print('ACQUIRED')\n"
        )
        assert loser.returncode != 0, "a second live process must not acquire ownership"
        assert "QueueOwnershipError" in loser.stderr
        assert "ACQUIRED" not in loser.stdout
    finally:
        holder.kill()
        holder.wait()


def test_second_process_cannot_recover_the_same_live_job(tmp_path: Path) -> None:
    """The concrete harm: two processes recovering/executing one row.

    The owner holds a PENDING row and ownership.  A contender must be refused at
    the ownership gate, so it never reaches ``resume_pending`` and the row is
    not claimed twice.
    """
    db = str(tmp_path / "jobs.sqlite3")
    cx = __import__("sqlite3").connect(db)
    holder_script = f"""
import asyncio
from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
q = InProcessJobQueue({db!r})

async def slow(payload):
    await asyncio.sleep(60)   # the owner keeps the attempt in flight
    return {{}}

async def main():
    q.register_handler('some_job', slow)
    await q.acquire_ownership()
    job_id = await q.enqueue(job_type='some_job', idempotency_key='recover-once', payload={{}})
    # let the owner's worker reserve the attempt before announcing readiness
    for _ in range(500):
        if (await q.get_status(job_id)).value == 'processing':
            break
        await asyncio.sleep(0.01)
    print('READY', job_id, flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""
    holder = _spawn(holder_script)
    try:
        line = holder.stdout.readline() if holder.stdout else ""
        assert line.startswith("READY"), f"holder failed: {holder.stderr.read()}"
        job_id = line.split()[1]

        contender = _run(
            "import asyncio, sys\n"
            "from nexus_ai_agent.adapters.in_process_job_queue import (\n"
            "    InProcessJobQueue, QueueOwnershipError,\n"
            ")\n"
            f"q = InProcessJobQueue({db!r})\n"
            "async def main():\n"
            "    try:\n"
            "        await q.acquire_ownership()\n"
            "    except QueueOwnershipError:\n"
            "        print('REFUSED')\n"
            "        return 1\n"
            "    recovered = await q.resume_pending()\n"
            "    print('RECOVERED', recovered)\n"
            "    return 0\n"
            "raise SystemExit(asyncio.run(main()))\n"
        )
        assert contender.returncode == 1, "the contender must be refused at the ownership gate"
        assert "REFUSED" in contender.stdout
        assert "RECOVERED" not in contender.stdout

        row = cx.execute(
            "SELECT status, attempt FROM nexus_job_queue WHERE id = ?", (job_id,)
        ).fetchone()
        assert row is not None
        # The owner reserved attempt 1 and is running it; the refused contender
        # must not have minted attempt 2 or reopened the row underneath it.
        assert row[0] == "processing", f"owner's attempt must stay in flight, got {row[0]}"
        assert row[1] == 1, f"no second reservation may be minted, got attempt {row[1]}"
    finally:
        cx.close()
        holder.kill()
        holder.wait()


# ── crash release ────────────────────────────────────────────────────────────


def test_killed_owner_releases_its_lock(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.sqlite3")
    holder = _spawn(_HOLDER_TEMPLATE.format(db=db, sleep=60))
    try:
        _wait_ready(holder)
        holder.kill()  # SIGKILL: no finally, no shutdown, no release call
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:  # pragma: no cover
            holder.kill()
            holder.wait()

    successor = _run(
        "import asyncio\n"
        "from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue\n"
        f"q = InProcessJobQueue({db!r})\n"
        "asyncio.run(q.acquire_ownership())\n"
        "print('ACQUIRED')\n"
    )
    assert successor.returncode == 0, successor.stderr
    assert "ACQUIRED" in successor.stdout


# ── the race the old primitive lost ──────────────────────────────────────────


def test_simultaneous_contenders_yield_exactly_one_owner(tmp_path: Path) -> None:
    """N processes race for one sidecar: exactly one wins, always.

    This is the regression for the O_CREAT|O_EXCL implementation, where two
    contenders could both observe the same stale lock, both ``unlink`` it and
    both create their own — two owners for one queue.  With an OS lock there is
    no reclaim step to race: the kernel serialises them.

    Repeated so a probabilistic window would show up.
    """
    rounds = 8
    for i in range(rounds):
        db = str(tmp_path / f"race_{i}.sqlite3")
        script = (
            "import asyncio\n"
            "from nexus_ai_agent.adapters.in_process_job_queue import (\n"
            "    InProcessJobQueue, QueueOwnershipError,\n"
            ")\n"
            f"q = InProcessJobQueue({db!r})\n"
            "async def main():\n"
            "    try:\n"
            "        await q.acquire_ownership()\n"
            "    except QueueOwnershipError:\n"
            "        print('LOSER')\n"
            "        return 2\n"
            "    print('WINNER', flush=True)\n"
            "    await asyncio.sleep(1.2)\n"
            "    return 0\n"
            "raise SystemExit(asyncio.run(main()))\n"
        )

        # Seed a *stale* lock anchor so every contender sees the same starting
        # state (exactly the condition the old reclamation path mishandled).
        Path(db + ".lock").write_text("0\n")

        procs = [_spawn(script) for _ in range(4)]
        outputs: list[str] = []
        for proc in procs:
            out, _err = proc.communicate(timeout=30)
            outputs.append(out)
        winners = sum(1 for out in outputs if "WINNER" in out)
        assert winners == 1, f"round {i}: expected exactly one owner, saw {winners} — {outputs}"
