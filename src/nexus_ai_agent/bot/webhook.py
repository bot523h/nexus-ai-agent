"""Webhook run-mode plumbing for scale-to-zero deployments (v3.8.0, Phase 3).

This module owns the *orchestration* of webhook mode — running the bot as an
HTTP service that Telegram POSTs updates to, instead of the always-on
long-polling loop.  This is what makes zero-idle-cost deployments possible on
platforms that scale a web service to zero (e.g. a Koyeb ``web`` service).

Responsibilities:

* :func:`resolve_run_mode` — CLI argument > ``NEXUS_RUN_MODE`` > ``"polling"``;
* :func:`build_webhook_bind` — bind address with port priority
  ``PORT`` > ``DASHBOARD_PORT`` > ``8000`` (PaaS platforms such as Koyeb
  inject ``PORT``);
* :func:`run_webhook` — initialize the PTB application, register the webhook
  with a shared secret header, serve the FastAPI app with uvicorn, and shut
  down gracefully on SIGTERM (scale-to-zero platforms SIGTERM the process on
  scale-in; they do not wait politely forever).

Import boundary: this file must NOT import the ``telegram`` package — the
frozen import-boundary test (``tests/architecture/test_import_boundaries.py``)
only tolerates ``telegram`` in grandfathered files.  Raw webhook payloads are
converted into PTB ``Update`` objects by ``WebhookApplicationAdapter`` in
``bot/app.py``, which is one of those grandfathered files.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any

from nexus_ai_agent.config.settings import Settings, get_settings

DEFAULT_RUN_MODE = "polling"
VALID_RUN_MODES = ("polling", "webhook")
DEFAULT_WEBHOOK_PORT = 8000

#: Header Telegram uses to echo back the ``secret_token`` given to
#: ``set_webhook``.  ``api/app.py`` compares it (constant-time) against
#: ``NEXUS_WEBHOOK_SECRET`` on every delivery.
TELEGRAM_SECRET_TOKEN_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_UVICORN_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


class WebhookConfigError(RuntimeError):
    """Webhook mode was selected but is not configured correctly."""


_LifecycleFailure = tuple[str, BaseException]
_LifecycleCleanupResult = tuple[list[_LifecycleFailure], list[_LifecycleFailure]]


class WebhookLifecycleError(RuntimeError):
    """One or more webhook application cleanup steps failed."""

    def __init__(
        self,
        primary_failure: BaseException | None,
        cleanup_failures: tuple[_LifecycleFailure, ...] | list[_LifecycleFailure],
    ) -> None:
        self.primary_failure = primary_failure
        self.cleanup_failures = tuple(cleanup_failures)
        details: list[str] = []
        if primary_failure is not None:
            details.append(f"primary {type(primary_failure).__name__}: {primary_failure}")
        details.extend(
            f"{phase} {type(failure).__name__}: {failure}"
            for phase, failure in self.cleanup_failures
        )
        super().__init__("webhook lifecycle cleanup failed: " + "; ".join(details))


async def _invoke_application_hook(application: Any, hook_name: str) -> None:
    """Dispatch one of PTB's registered lifecycle callbacks, if configured."""
    hook = getattr(application, hook_name, None)
    if hook is not None:
        await hook(application)


class _WebhookLifecycle:
    """Exactly-once callback adapter for one manually-driven PTB lifecycle."""

    _HOOKS = frozenset({"post_init", "post_stop", "post_shutdown"})

    def __init__(self, application: Any) -> None:
        self._application = application
        self._attempted: set[str] = set()
        self._completed: set[str] = set()

    async def invoke_hook(self, hook_name: str) -> None:
        if hook_name not in self._HOOKS:
            raise ValueError(f"unsupported PTB lifecycle hook: {hook_name!r}")
        if hook_name in self._attempted or hook_name in self._completed:
            return

        self._attempted.add(hook_name)
        try:
            await _invoke_application_hook(self._application, hook_name)
        except BaseException:
            # Startup hooks can have partially-mutated application state and
            # must not be repeated. Cleanup callbacks, in contrast, stay
            # retryable on this lifecycle object if they fail or are cancelled.
            if hook_name != "post_init":
                self._attempted.discard(hook_name)
            raise
        self._completed.add(hook_name)


def resolve_run_mode(cli_value: str | None = None) -> str:
    """Resolve the run mode: CLI argument > ``NEXUS_RUN_MODE`` > ``"polling"``.

    An empty/whitespace CLI value counts as "not provided" (the CLI flag
    defaults to ``None`` so the environment can win when the flag is
    omitted).  Unknown values raise ``ValueError``.
    """
    value = (cli_value or "").strip().lower()
    if not value:
        value = os.environ.get("NEXUS_RUN_MODE", "").strip().lower()
    if not value:
        value = DEFAULT_RUN_MODE
    if value not in VALID_RUN_MODES:
        raise ValueError(
            f"unknown run mode: {value!r} (expected one of: {', '.join(VALID_RUN_MODES)})"
        )
    return value


def resolve_webhook_port() -> int:
    """HTTP port for webhook mode: ``PORT`` > ``DASHBOARD_PORT`` > ``8000``.

    The first variable that is set wins; an unset variable falls through to
    the next candidate.  A set-but-non-integer value is a configuration
    error and raises ``ValueError`` (fail fast rather than silently binding
    somewhere unexpected).
    """
    for var in ("PORT", "DASHBOARD_PORT"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        return int(raw)
    return DEFAULT_WEBHOOK_PORT


def build_webhook_bind() -> tuple[str, int]:
    """Return the ``(host, port)`` uvicorn should bind for webhook mode.

    The host defaults to ``0.0.0.0`` because webhook mode exists precisely
    to be reached from outside a container; ``DASHBOARD_HOST`` may override
    it.  Port priority: ``PORT`` > ``DASHBOARD_PORT`` > ``8000``.
    """
    host = os.environ.get("DASHBOARD_HOST", "").strip() or "0.0.0.0"
    return host, resolve_webhook_port()


def run_webhook(application: Any, *, settings: Settings | None = None) -> None:
    """Run the bot in webhook mode, blocking until its shared lifecycle closes.

    Unlike PTB's runner, this adapter serves the existing FastAPI application
    with Uvicorn. It therefore manually drives the same registered PTB hooks as
    polling: ``post_init`` after initialization, then ``post_stop`` and
    ``post_shutdown`` during cleanup. Preflight failures still invoke
    ``post_shutdown`` so resources created with the application are not leaked.
    """
    selected_settings = settings if settings is not None else get_settings()
    asyncio.run(_run_webhook(application, selected_settings))


async def _attempt_cleanup(
    phase: str,
    operation: Callable[[], Awaitable[Any]],
    failures: list[_LifecycleFailure],
    cancellations: list[_LifecycleFailure],
) -> bool:
    try:
        await operation()
    except asyncio.CancelledError as exc:
        cancellations.append((phase, exc))
        return False
    except BaseException as exc:
        failures.append((phase, exc))
        return False
    return True


async def _cleanup_incomplete_ptb_shutdown(
    application: Any,
    failures: list[_LifecycleFailure],
    cancellations: list[_LifecycleFailure],
) -> None:
    """Close PTB's public components if its aggregate shutdown did not finish."""
    for name, component in (
        ("bot", getattr(application, "bot", None)),
        ("update_processor", getattr(application, "update_processor", None)),
        ("updater", getattr(application, "updater", None)),
    ):
        shutdown = getattr(component, "shutdown", None)
        if shutdown is not None:
            await _attempt_cleanup(
                f"{name}.shutdown.fallback",
                shutdown,
                failures,
                cancellations,
            )


async def _cleanup_application_lifecycle(
    application: Any,
    lifecycle: _WebhookLifecycle,
    *,
    initialize_attempted: bool,
    initialize_succeeded: bool,
    start_attempted: bool,
) -> _LifecycleCleanupResult:
    """Attempt every reachable PTB shutdown phase in dependency order."""
    failures: list[_LifecycleFailure] = []
    cancellations: list[_LifecycleFailure] = []

    if start_attempted:
        await _attempt_cleanup("application.stop", application.stop, failures, cancellations)
        await _attempt_cleanup(
            "post_stop",
            lambda: lifecycle.invoke_hook("post_stop"),
            failures,
            cancellations,
        )
    if initialize_attempted:
        application_shutdown_succeeded = await _attempt_cleanup(
            "application.shutdown", application.shutdown, failures, cancellations
        )
        if not initialize_succeeded or not application_shutdown_succeeded:
            await _cleanup_incomplete_ptb_shutdown(application, failures, cancellations)
    await _attempt_cleanup(
        "post_shutdown",
        lambda: lifecycle.invoke_hook("post_shutdown"),
        failures,
        cancellations,
    )
    return failures, cancellations


async def _await_cleanup_shielded(
    cleanup: Coroutine[Any, Any, _LifecycleCleanupResult],
) -> tuple[asyncio.CancelledError | None, _LifecycleCleanupResult]:
    """Finish cleanup despite repeated cancellation of the serving task."""
    cleanup_task = asyncio.create_task(cleanup, name="nexus-webhook-lifecycle-cleanup")
    caller_cancellation: asyncio.CancelledError | None = None

    while True:
        try:
            return caller_cancellation, await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            if cleanup_task.done():
                try:
                    return caller_cancellation or exc, cleanup_task.result()
                except asyncio.CancelledError as cleanup_cancellation:
                    return caller_cancellation, (
                        [],
                        [("cleanup task", cleanup_cancellation)],
                    )
                except BaseException as cleanup_failure:
                    return caller_cancellation or exc, (
                        [("cleanup task", cleanup_failure)],
                        [],
                    )
            if caller_cancellation is None:
                caller_cancellation = exc
        except BaseException as cleanup_failure:
            return caller_cancellation, ([("cleanup task", cleanup_failure)], [])


def _raise_lifecycle_outcome(
    primary_failure: BaseException | None,
    caller_cancellation: asyncio.CancelledError | None,
    cleanup_result: _LifecycleCleanupResult,
) -> None:
    failures, cancellations = cleanup_result
    all_failures = [*failures, *cancellations]
    if caller_cancellation is not None:
        all_failures.append(("serving task cancellation", caller_cancellation))

    primary_cancellation = (
        primary_failure if isinstance(primary_failure, asyncio.CancelledError) else None
    )
    cleanup_cancellation = next(
        (failure for _, failure in cancellations if isinstance(failure, asyncio.CancelledError)),
        None,
    )
    cancellation = primary_cancellation or caller_cancellation or cleanup_cancellation
    if cancellation is not None:
        cause_failures = [
            (phase, failure) for phase, failure in all_failures if failure is not cancellation
        ]
        cause_primary = primary_failure if primary_failure is not primary_cancellation else None
        if cause_primary is not None or cause_failures:
            raise cancellation from WebhookLifecycleError(cause_primary, cause_failures)
        raise cancellation

    if primary_failure is not None:
        if all_failures:
            if isinstance(primary_failure, Exception):
                raise WebhookLifecycleError(primary_failure, all_failures) from primary_failure
            raise primary_failure.with_traceback(primary_failure.__traceback__) from (
                WebhookLifecycleError(None, all_failures)
            )
        raise primary_failure.with_traceback(primary_failure.__traceback__)

    if all_failures:
        fatal_failure = next(
            (failure for _, failure in all_failures if not isinstance(failure, Exception)),
            None,
        )
        if fatal_failure is not None:
            other_failures = [
                (phase, failure) for phase, failure in all_failures if failure is not fatal_failure
            ]
            if other_failures:
                raise fatal_failure from WebhookLifecycleError(None, other_failures)
            raise fatal_failure
        raise WebhookLifecycleError(None, all_failures)


@contextmanager
def _defer_uvicorn_signal_reraise(server: Any) -> Iterator[None]:
    """Keep Uvicorn's post-serve signal re-raise from skipping app cleanup.

    Uvicorn captures SIGINT/SIGTERM around ``Server.serve()``, restores the
    preexisting handlers, and re-raises the captured signals before returning.
    Our outer handler defers that re-raise until PTB/runtime cleanup finishes,
    then replays it so the process retains Uvicorn's normal signal exit status.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    handled = tuple(
        sig
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
        if sig is not None
    )
    original_handlers: dict[Any, Any] = {}
    deferred_signals: list[Any] = []

    def defer_signal(signum: Any, _frame: Any) -> None:
        deferred_signals.append(signum)
        server.should_exit = True

    try:
        for sig in handled:
            original_handlers[sig] = signal.signal(sig, defer_signal)
    except (OSError, ValueError):
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)
        yield
        return

    try:
        yield
    finally:
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)
        for sig in reversed(deferred_signals):
            signal.raise_signal(sig)


async def _serve_uvicorn_cancellation_safe(server: Any) -> None:
    """Let Uvicorn finish its ASGI shutdown before propagating cancellation."""
    server_task = asyncio.create_task(server.serve(), name="nexus-webhook-uvicorn")
    cancellation: asyncio.CancelledError | None = None
    server_failure: BaseException | None = None

    while True:
        try:
            await asyncio.shield(server_task)
            break
        except asyncio.CancelledError as exc:
            if server_task.done():
                try:
                    server_task.result()
                except asyncio.CancelledError as server_cancellation:
                    cancellation = cancellation or server_cancellation
                except BaseException as exc:
                    server_failure = exc
                else:
                    cancellation = cancellation or exc
                break
            cancellation = cancellation or exc
            server.should_exit = True
        except BaseException as exc:
            server_failure = exc
            break

    if cancellation is not None:
        if server_failure is not None:
            raise cancellation from WebhookLifecycleError(
                None,
                [("uvicorn.serve", server_failure)],
            )
        raise cancellation
    if server_failure is not None:
        raise server_failure.with_traceback(server_failure.__traceback__)


async def _run_webhook(application: Any, settings: Settings) -> None:
    lifecycle = _WebhookLifecycle(application)
    try:
        webhook_url = (settings.webhook_url or "").strip()
        webhook_secret = (settings.webhook_secret or "").strip()
        if not webhook_url:
            raise WebhookConfigError(
                "webhook mode requires NEXUS_WEBHOOK_URL "
                "(public HTTPS URL Telegram should POST updates to)"
            )
        if not webhook_secret:
            raise WebhookConfigError(
                "webhook mode requires NEXUS_WEBHOOK_SECRET "
                "(shared secret echoed by Telegram in " + TELEGRAM_SECRET_TOKEN_HEADER + ")"
            )

        host, port = build_webhook_bind()
        from nexus_ai_agent.api.app import app as api_app

        api_app.state.webhook_application = application
        api_app.state.webhook_secret = webhook_secret
        log_level = settings.log_level
    except BaseException as primary_failure:
        caller_cancellation, cleanup_result = await _await_cleanup_shielded(
            _cleanup_application_lifecycle(
                application,
                lifecycle,
                initialize_attempted=False,
                initialize_succeeded=False,
                start_attempted=False,
            )
        )
        _raise_lifecycle_outcome(primary_failure, caller_cancellation, cleanup_result)
        return

    await _serve_webhook(
        application=application,
        api_app=api_app,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        host=host,
        port=port,
        log_level=log_level,
        _lifecycle=lifecycle,
    )


async def _serve_webhook(
    *,
    application: Any,
    api_app: Any,
    webhook_url: str,
    webhook_secret: str,
    host: str,
    port: int,
    log_level: str,
    _lifecycle: _WebhookLifecycle | None = None,
) -> None:
    """Serve webhook mode on a running event loop (see :func:`run_webhook`)."""
    lifecycle = _lifecycle or _WebhookLifecycle(application)
    initialize_attempted = False
    initialize_succeeded = False
    start_attempted = False
    primary_failure: BaseException | None = None

    try:
        import uvicorn

        # Prepare the server before acquiring PTB resources so configuration
        # failures can still close the prebuilt runtime without partial startup.
        level = log_level.strip().lower()
        config = uvicorn.Config(
            api_app,
            host=host,
            port=port,
            log_level=level if level in _UVICORN_LOG_LEVELS else None,
        )
        server = uvicorn.Server(config)
    except BaseException as exc:
        server = None
        primary_failure = exc

    if server is None:
        caller_cancellation, cleanup_result = await _await_cleanup_shielded(
            _cleanup_application_lifecycle(
                application,
                lifecycle,
                initialize_attempted=False,
                initialize_succeeded=False,
                start_attempted=False,
            )
        )
        _raise_lifecycle_outcome(primary_failure, caller_cancellation, cleanup_result)
        return

    # Uvicorn re-raises captured SIGINT/SIGTERM after serve() returns. Keep an
    # outer deferring handler installed through PTB/runtime cleanup so that
    # this re-raise (or a second signal during cleanup) cannot kill the process
    # before its owned resources reach a terminal state.
    with _defer_uvicorn_signal_reraise(server):
        try:
            # Mirror PTB's callback contract. post_init performs reminder
            # binding and job recovery; do not run those engines a second time.
            initialize_attempted = True
            await application.initialize()
            initialize_succeeded = True
            await lifecycle.invoke_hook("post_init")

            start_attempted = True
            await application.start()

            # A signal arriving during startup sets should_exit on the server;
            # don't publish a new Telegram webhook if shutdown is already due.
            if not server.should_exit:
                await application.bot.set_webhook(
                    url=webhook_url,
                    secret_token=webhook_secret,
                )
                await _serve_uvicorn_cancellation_safe(server)
        except BaseException as exc:
            primary_failure = exc

        caller_cancellation, cleanup_result = await _await_cleanup_shielded(
            _cleanup_application_lifecycle(
                application,
                lifecycle,
                initialize_attempted=initialize_attempted,
                initialize_succeeded=initialize_succeeded,
                start_attempted=start_attempted,
            )
        )
        _raise_lifecycle_outcome(primary_failure, caller_cancellation, cleanup_result)
