"""Safe subprocess governance: deadline, cancellation, kill, bounded pipes.

The repository already runs FFmpeg from :mod:`nexus_ai_agent.creative.slideshow.ffmpeg`
and :mod:`nexus_ai_agent.creative.rendering.executor` with ``subprocess.run(...,
timeout=...)``.  That is enough for a single encode, and it is **not** enough
for a long-form engine, for three concrete reasons:

1. ``subprocess.run`` communicates through pipes and only returns once the child
   exits; a child that inherits the pipe and hangs takes the timeout path with
   an unread pipe, and the captured output is unbounded in principle;
2. there is no cancellation handle — a user pressing "stop" on a 40-minute
   render has to wait for the deadline;
3. a timeout raised by ``run`` does not prove the process group is gone.

This module is the bounded runner the engine uses.  It does **one** thing —
run an allow-listed argv to completion, or kill it and say so:

* **one process group per call** (``start_new_session=True``) so
  :func:`os.killpg` reaches FFmpeg *and* everything it spawned;
* **a deadline** enforced by a watchdog thread; on expiry the group is
  ``SIGKILL``-ed and the outcome says ``termination="timeout"`` — never a silent
  partial success;
* **cancellation** through a cooperative :class:`CancelToken`, checked by the
  watchdog, so an in-flight long render stops within one poll interval;
* **bounded pipes** — stdout/stderr are drained by threads into a ring buffer
  capped at ``max_captured_bytes``, so a chatty process can never grow memory
  without bound (the full text is truncated, the tail is what we keep, and the
  cap is reported on the outcome);
* **no shell, no ``-y``, no PATH lookup surprises**: the caller passes an argv
  whose binary was resolved by
  :func:`nexus_ai_agent.creative.slideshow.ffmpeg.resolve_ffmpeg_bin`.

Time accounting: every call returns ``elapsed_seconds`` measured with
``time.monotonic`` around the actual process lifetime, and the runner's budget
hook records it, so ``ResourceBudget`` numbers are measurements, not estimates.
"""

from __future__ import annotations

import os
import signal
import subprocess  # noqa: S404 - argv lists, no shell; the point of this module
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from nexus_ai_agent.creative.audio_engine.models import ProcessOutcome

__all__ = [
    "CancelToken",
    "ProcessGovernor",
    "ProcessTimeout",
    "ProcessCancelled",
    "ProcessLaunchError",
    "GovernedProcess",
]

#: How often the watchdog checks the deadline / cancellation flag.
_POLL_SECONDS = 0.02
#: Tail bytes kept per stream (bounded memory, enough for a real error message).
DEFAULT_MAX_CAPTURED_BYTES = 256 * 1024


class GovernedProcessError(RuntimeError):
    """Base class for every bounded-runner failure."""


class ProcessTimeout(GovernedProcessError):
    """The process did not finish inside its deadline and was killed."""


class ProcessCancelled(GovernedProcessError):
    """The caller's cancel token fired and the process was killed."""


class ProcessLaunchError(GovernedProcessError):
    """The binary could not be started at all."""


class CancelToken:
    """A thread-safe cooperative cancellation flag.

    Deliberately tiny: one flag, one reason, idempotent.  It is *not* an
    ``asyncio.Event`` because the engine's execution plane is synchronous (the
    job queue runs it on a worker thread) and a token crossing threads is the
    only shape that works there.
    """

    __slots__ = ("_event", "_reason", "_lock")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()

    def cancel(self, reason: str = "cancelled by caller") -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ProcessCancelled(self._reason or "cancelled by caller")


@dataclass
class _BoundedSink:
    """Drain a pipe into a capped tail buffer (bytes kept, total counted)."""

    limit: int
    chunks: bytearray = field(default_factory=bytearray)
    total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def drain(self, pipe: object) -> None:
        read = getattr(pipe, "read", None)
        if read is None:  # pragma: no cover - defensive: closed before start
            return
        while True:
            chunk = read(16384)
            if not chunk:
                break
            with self._lock:
                self.total += len(chunk)
                self.chunks.extend(chunk)
                overflow = len(self.chunks) - self.limit
                if overflow > 0:
                    del self.chunks[:overflow]

    @property
    def tail(self) -> str:
        with self._lock:
            return bytes(self.chunks).decode("utf-8", errors="replace")


class GovernedProcess(Protocol):
    """The seam the engine programs against (so tests can inject a fake)."""

    def run(
        self,
        argv: Sequence[str],
        *,
        deadline_seconds: float,
        cancel: CancelToken | None = None,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
        workdir: str | None = None,
        max_captured_bytes: int = DEFAULT_MAX_CAPTURED_BYTES,
    ) -> ProcessOutcome: ...


class ProcessGovernor:
    """The bounded runner described in the module docstring.

    ``clock`` is injectable so a test can drive the deadline deterministically
    without sleeping; production uses :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        # ``clock`` is typed as a callable on purpose: it is called on every
        # poll to enforce the deadline, so a non-callable would surface as a
        # TypeError mid-run instead of at construction.
        self._clock = clock
        self._poll = max(0.001, poll_seconds)
        self._processes_started = 0
        self._lock = threading.Lock()

    @property
    def processes_started(self) -> int:
        with self._lock:
            return self._processes_started

    def run(
        self,
        argv: Sequence[str],
        *,
        deadline_seconds: float,
        cancel: CancelToken | None = None,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
        workdir: str | None = None,
        max_captured_bytes: int = DEFAULT_MAX_CAPTURED_BYTES,
    ) -> ProcessOutcome:
        if not argv:
            raise ProcessLaunchError("argv must not be empty")
        if deadline_seconds <= 0:
            raise ProcessTimeout("deadline_seconds must be positive")
        if cancel is not None and cancel.cancelled:
            raise ProcessCancelled(cancel.reason or "cancelled before start")
        if stdout_path is not None:
            # The engine never lets FFmpeg own the destination path: the only
            # safe shape is "write stdout to a staging file descriptor".
            parent = os.path.dirname(os.path.abspath(stdout_path))
            if parent and not os.path.isdir(parent):
                raise ProcessLaunchError(f"stdout directory does not exist: {parent}")

        argv_list = list(argv)
        started = self._clock()
        stdout_sink = _BoundedSink(limit=max_captured_bytes)
        stderr_sink = _BoundedSink(limit=max_captured_bytes)
        out_handle = None
        in_handle = None
        try:
            try:
                if stdin_path is not None:
                    in_handle = open(stdin_path, "rb")  # noqa: SIM115 - closed below
                if stdout_path is not None:
                    out_handle = open(stdout_path, "wb")  # noqa: SIM115 - closed below
                process = subprocess.Popen(  # noqa: S603 - argv list, no shell
                    argv_list,
                    stdin=in_handle if in_handle is not None else subprocess.DEVNULL,
                    stdout=out_handle if out_handle is not None else subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                    cwd=workdir,
                    close_fds=True,
                )
            except (OSError, ValueError) as error:
                raise ProcessLaunchError(f"could not start {argv_list[0]!r}: {error}") from error
            with self._lock:
                self._processes_started += 1
            # The child holds its own duplicate now; our descriptors can close so
            # no leak outlives the call.
            for handle in (in_handle, out_handle):
                if handle is not None:
                    handle.close()
            in_handle = out_handle = None

            drain_threads: list[threading.Thread] = []
            if process.stdout is not None:
                thread = threading.Thread(
                    target=stdout_sink.drain, args=(process.stdout,), daemon=True
                )
                thread.start()
                drain_threads.append(thread)
            if process.stderr is not None:
                thread = threading.Thread(
                    target=stderr_sink.drain, args=(process.stderr,), daemon=True
                )
                thread.start()
                drain_threads.append(thread)

            termination = "exited"
            detail: str | None = None
            returncode: int | None = None
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                now = self._clock()
                if now - started >= deadline_seconds:
                    termination = "timeout"
                    detail = f"deadline of {deadline_seconds:g}s reached; process group killed"
                    self._kill_group(process)
                    returncode = process.poll()
                    break
                if cancel is not None and cancel.cancelled:
                    termination = "cancelled"
                    detail = cancel.reason or "cancelled by caller"
                    self._kill_group(process)
                    returncode = process.poll()
                    break
                time.sleep(self._poll)

            # The group is gone (or exited); joining the drains bounds the call.
            for thread in drain_threads:
                thread.join(timeout=max(1.0, self._poll * 50))
            self._close_pipes(process)
            elapsed = self._clock() - started
            outcome = ProcessOutcome(
                argv=tuple(argv_list),
                returncode=returncode,
                termination=termination,  # type: ignore[arg-type]
                elapsed_seconds=round(float(elapsed), 6),
                stdout_tail=stdout_sink.tail[-4000:],
                stderr_tail=stderr_sink.tail[-8000:],
                captured_bytes=stdout_sink.total + stderr_sink.total,
                detail=detail,
            )
            if termination == "timeout":
                raise _TimeoutWithOutcome(outcome)
            if termination == "cancelled":
                raise _CancelledWithOutcome(outcome)
            return outcome
        finally:
            for handle in (in_handle, out_handle):
                if handle is not None:
                    handle.close()

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        """SIGKILL the whole group, then reap it.  Never raises."""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
            process.kill()
        except OSError:  # pragma: no cover - defensive
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
            pass

    @staticmethod
    def _close_pipes(process: subprocess.Popen[bytes]) -> None:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:  # pragma: no cover - already closed by the drain
                    pass


class _TimeoutWithOutcome(ProcessTimeout):
    """Timeout carrying the measured outcome (time, tail) for evidence."""

    def __init__(self, outcome: ProcessOutcome) -> None:
        super().__init__(outcome.detail or "process timed out")
        self.outcome = outcome


class _CancelledWithOutcome(ProcessCancelled):
    """Cancellation carrying the measured outcome (time, tail) for evidence."""

    def __init__(self, outcome: ProcessOutcome) -> None:
        super().__init__(outcome.detail or "cancelled by caller")
        self.outcome = outcome
