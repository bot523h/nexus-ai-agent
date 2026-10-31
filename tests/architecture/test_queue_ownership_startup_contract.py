"""task-283 — architecture gate: every composition root acquires ownership first.

The queue cannot know whether it is being started by a composition root or
constructed by a test, so the *ordering* guarantee lives at the roots.  That
makes it a structural invariant worth enforcing structurally: for every entry
point that opens the queue sidecar, the call to ``acquire_ownership`` must
appear before any recovery call (``resume_pending`` / ``resume_pending_jobs``),
and the same root must stop the queue (which stops workers and heartbeat
children, then releases the lock).

This is checked on the AST, so a commented-out or string-mentioned call cannot
satisfy it, and no mock can hide a regression.  Root causes this pins (see
docs/architecture/JOB_LIFECYCLE.md):

* ``bot/app.py::_post_init`` called ``resume_pending()`` with no ownership at all,
* ``bot/webhook.py::_serve_webhook`` did the same,
* ``cli.py``'s drain and one-shot intent runner executed jobs unowned.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "nexus_ai_agent"
QUEUE_MODULE = SOURCE_ROOT / "adapters" / "in_process_job_queue.py"

_ACQUIRE = "acquire_ownership"

#: ``(file, function, work it must not begin before owning the queue)``.
#:
#: Recovery roots must acquire before ``resume_pending*``; the one-shot runner
#: must acquire before it enqueues/executes anything at all.
GUARDED_ROOTS = [
    ("bot/app.py", "_post_init", {"resume_pending", "resume_pending_jobs"}),
    ("bot/webhook.py", "_serve_webhook", {"resume_pending", "resume_pending_jobs"}),
    ("cli.py", "_run", {"resume_pending", "resume_pending_jobs"}),
    ("cli.py", "_run_all", {"run_free_text_intent_durable", "enqueue"}),
]

#: ``(file, acquiring function, function that stops the queue)``.
RELEASE_SITES = [
    ("bot/app.py", "_post_init", "_post_shutdown"),
    ("bot/webhook.py", "_serve_webhook", "_cleanup_webhook_application"),
    ("cli.py", "_run", "_run"),
    ("cli.py", "_run_all", "_run_all"),
]


def _functions(tree: ast.AST, name: str) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def _call_lines(node: ast.AST, names: set[str]) -> list[int]:
    """Sorted line numbers of every call to one of ``names`` inside ``node``."""
    lines: list[int] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called in names:
            lines.append(sub.lineno)
    return sorted(lines)


def _parse(relpath: str) -> ast.AST:
    path = SOURCE_ROOT / relpath
    assert path.exists(), f"composition root disappeared: {relpath}"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize(("relpath", "func", "guarded"), GUARDED_ROOTS)
def test_root_acquires_ownership_before_guarded_work(
    relpath: str, func: str, guarded: set[str]
) -> None:
    tree = _parse(relpath)
    # A name can occur more than once in a module (``cli.py`` has two ``_run``
    # helpers); the gate applies to every definition that performs guarded work.
    candidates = [node for node in _functions(tree, func) if _call_lines(node, guarded)]
    assert candidates, f"{relpath}::{func} no longer performs {guarded} — update this gate"

    for node in candidates:
        acquire_lines = _call_lines(node, {_ACQUIRE})
        guarded_lines = _call_lines(node, guarded)
        assert acquire_lines, (
            f"{relpath}::{func} (line {node.lineno}) never calls acquire_ownership(); "
            "guarded work would run under uncertain process ownership"
        )
        assert min(acquire_lines) < min(guarded_lines), (
            f"{relpath}::{func} (line {node.lineno}) does guarded work at line "
            f"{min(guarded_lines)} before acquiring ownership at line {min(acquire_lines)}"
        )


@pytest.mark.parametrize(("relpath", "acquirer", "stopper"), RELEASE_SITES)
def test_root_stops_the_queue_and_releases(relpath: str, acquirer: str, stopper: str) -> None:
    """Every root that acquires ownership must also reach a queue shutdown."""
    tree = _parse(relpath)
    owning = [node for node in _functions(tree, acquirer) if _call_lines(node, {_ACQUIRE})]
    assert owning, f"{relpath}::{acquirer} does not acquire ownership"

    stop_sites = _functions(tree, stopper)
    assert stop_sites, f"{relpath}::{stopper} not found"
    assert any(_call_lines(node, {"shutdown"}) for node in stop_sites), (
        f"{relpath}::{stopper} never calls shutdown(); workers keep running and the process "
        "lock is never released"
    )


def test_failed_acquisition_is_fatal_not_degraded() -> None:
    """Ownership failure must abort startup — never fall through to a runner."""
    tree = _parse("bot/app.py")
    post_init = [n for n in _functions(tree, "_post_init") if _call_lines(n, {_ACQUIRE})]
    assert post_init, "bot/app.py::_post_init does not acquire ownership"
    source = (SOURCE_ROOT / "bot" / "app.py").read_text(encoding="utf-8")

    # The acquisition is wrapped in an except that raises, not one that logs.
    raises_after_ownership_error = False
    for node in ast.walk(post_init[0]):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = node.type
        names = (
            [caught.attr]
            if isinstance(caught, ast.Attribute)
            else [e.id for e in getattr(caught, "elts", []) if isinstance(e, ast.Name)]
            or ([caught.id] if isinstance(caught, ast.Name) else [])
        )
        if "QueueOwnershipError" in names:
            raises_after_ownership_error = any(isinstance(sub, ast.Raise) for sub in ast.walk(node))
    assert raises_after_ownership_error, (
        "bot/app.py::_post_init catches QueueOwnershipError without raising: a process that "
        "cannot prove ownership must fail closed"
    )
    assert "queue_ownership" in source, "startup_status must record the ownership phase"


# ── the primitive itself ─────────────────────────────────────────────────────


def test_lock_primitive_is_an_os_lock() -> None:
    """Ownership must be an OS-managed lock, not PID bookkeeping."""
    tree = ast.parse(QUEUE_MODULE.read_text(encoding="utf-8"))

    descriptor = _functions(tree, "_lock_descriptor")
    assert descriptor, "_lock_descriptor (the OS lock primitive) is missing"
    assert _call_lines(descriptor[0], {"flock", "locking"}), (
        "_lock_descriptor does not call fcntl.flock / msvcrt.locking"
    )

    acquire = _functions(tree, "_acquire_flock")
    assert acquire, "_acquire_flock is missing"
    assert _call_lines(acquire[0], {"_lock_descriptor"}), "_acquire_flock does not take the OS lock"
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "O_EXCL" for node in ast.walk(acquire[0])
    ), "_acquire_flock still uses O_EXCL; that is PID-lock-file semantics, not a real lock"


def test_release_closes_the_descriptor_and_keeps_the_anchor() -> None:
    tree = ast.parse(QUEUE_MODULE.read_text(encoding="utf-8"))
    release = _functions(tree, "_release_flock")
    assert release, "_release_flock is missing"
    calls = set()
    for node in ast.walk(release[0]):
        if isinstance(node, ast.Call):
            func = node.func
            calls.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert "close" in calls, "_release_flock never closes the descriptor it opened"
    assert "_unlock_descriptor" in calls, "_release_flock never unlocks before closing"
    assert "unlink" not in calls, (
        "_release_flock unlinks the lock anchor; a concurrent opener can then lock a "
        "different inode while another still holds the old one"
    )
