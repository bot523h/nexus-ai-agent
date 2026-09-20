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
    """Run the bot in webhook mode (blocking until graceful shutdown).

    Steps: validate configuration → publish the application (and secret) on
    the FastAPI app state → initialize/start the PTB application → register
    the webhook with Telegram (echoing a shared secret) → serve with
    uvicorn.  SIGTERM/SIGINT make uvicorn stop accepting new requests and
    drain in-flight ones; afterwards the PTB application is stopped and shut
    down cleanly, which is what scale-to-zero needs when the platform
    terminates the process.
    """
    settings = settings if settings is not None else get_settings()
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

    from nexus_ai_agent.api.app import app as api_app

    api_app.state.webhook_application = application
    api_app.state.webhook_secret = webhook_secret

    host, port = build_webhook_bind()
    asyncio.run(
        _serve_webhook(
            application=application,
            api_app=api_app,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            host=host,
            port=port,
            log_level=settings.log_level,
        )
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
) -> None:
    """Serve webhook mode on a running event loop (see :func:`run_webhook`)."""
    import uvicorn

    # 1) Bring the application up.  No Updater is started, so nothing polls:
    #    Telegram POSTs updates to /webhook/telegram instead.
    await application.initialize()
    job_queue = getattr(application, "bot_data", {}).get("job_queue")
    if job_queue is not None:
        await job_queue.resume_pending()
    await application.start()
    try:
        # 2) Register the webhook.  Telegram will echo `webhook_secret` back
        #    in TELEGRAM_SECRET_TOKEN_HEADER on every delivery.
        await application.bot.set_webhook(url=webhook_url, secret_token=webhook_secret)

        # 3) Serve.  uvicorn installs SIGINT/SIGTERM handlers that set
        #    `should_exit`; the server then stops accepting connections and
        #    drains in-flight requests before serve() returns.
        level = log_level.strip().lower()
        config = uvicorn.Config(
            api_app,
            host=host,
            port=port,
            log_level=level if level in _UVICORN_LOG_LEVELS else None,
        )
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        # 4) Graceful application shutdown: stop consuming updates and close
        #    the bot's HTTP sessions.  Runs even if serving failed, so a
        #    platform-issued SIGTERM never leaves half-open resources behind.
        await application.stop()
        await application.shutdown()
