"""Webhook run-mode plumbing for scale-to-zero deployments (v3.8.0, Phase 3).

This module owns the *orchestration* of webhook mode — running the bot as an
HTTP service that Telegram POSTs updates to, instead of the always-on
long-polling loop. This is what makes zero-idle-cost deployments possible on
platforms that scale a web service to zero (e.g. a Koyeb ``web`` service).

Responsibilities:

* :func:`resolve_run_mode` — CLI argument > ``NEXUS_RUN_MODE`` > ``"polling"``;
* :func:`build_webhook_bind` — bind address with port priority
  ``PORT`` > ``DASHBOARD_PORT`` > ``8000`` (PaaS platforms such as Koyeb
  inject ``PORT``);
* :func:`run_webhook` — initialize the PTB application, register the webhook
  with a shared secret header, serve the FastAPI app with uvicorn, and shut
  down gracefully on SIGTERM.

Import boundary: this file must NOT import the ``telegram`` package — the
frozen import-boundary test (``tests/architecture/test_import_boundaries.py``)
only tolerates ``telegram`` in grandfathered files. Raw webhook payloads are
converted into PTB ``Update`` objects by ``WebhookApplicationAdapter`` in
``bot/app.py``, which is one of those grandfathered files.

W1 Fix: PTB's initialize()/shutdown() do NOT call post_init/post_shutdown
(only run_polling/run_webhook do). Since we manually orchestrate webhook via
FastAPI+uvicorn, we must explicitly call post_init after start and
post_shutdown before stop/shutdown. Also start() is now inside try/finally so
failure still triggers cleanup, and each shutdown step is fail-safe.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from nexus_ai_agent.config.settings import Settings, get_settings
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_RUN_MODE = "polling"
VALID_RUN_MODES = ("polling", "webhook")
DEFAULT_WEBHOOK_PORT = 8000

TELEGRAM_SECRET_TOKEN_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_UVICORN_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


class WebhookConfigError(RuntimeError):
    """Webhook mode was selected but is not configured correctly."""


def resolve_run_mode(cli_value: str | None = None) -> str:
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
    for var in ("PORT", "DASHBOARD_PORT"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        return int(raw)
    return DEFAULT_WEBHOOK_PORT


def build_webhook_bind() -> tuple[str, int]:
    host = os.environ.get("DASHBOARD_HOST", "").strip() or "0.0.0.0"
    return host, resolve_webhook_port()


def run_webhook(application: Any, *, settings: Settings | None = None) -> None:
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
    """Serve webhook mode on a running event loop.

    W1 canonical lifecycle (fixed):
    - PTB's initialize() does NOT run post_init, shutdown() does NOT run
      post_shutdown — only run_polling/run_webhook do. Since we manually
      orchestrate via uvicorn, we explicitly invoke post_init after start
      and post_shutdown before stop/shutdown.
    - start() is inside try so failure still triggers shutdown chain.
    - No duplicate resume_pending — post_init is the ONE authority.
    - Each shutdown step fail-safe: one failure never strands later resources.
    """
    import uvicorn

    # 1) Bring the application up — initialize creates bot, update_queue etc
    await application.initialize()

    try:
        # start() may fail (e.g., network) — must still cleanup, so inside try
        await application.start()

        # 2) Explicitly run post_init — canonical startup authority (W1 Law 4)
        # PTB would do this in run_webhook(), but we are not using run_webhook
        post_init = getattr(application, "post_init", None)
        if callable(post_init):
            try:
                await post_init(application)
            except Exception:
                logger.exception("webhook_post_init_failed")

        # 3) Register the webhook with Telegram
        await application.bot.set_webhook(url=webhook_url, secret_token=webhook_secret)

        # 4) Serve FastAPI with uvicorn — SIGTERM/SIGINT handled by uvicorn
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
        # 5) Graceful shutdown in canonical PTB order + explicit post_shutdown
        # post_shutdown runs Runtime.shutdown() which disposes ALL engines
        try:
            post_shutdown = getattr(application, "post_shutdown", None)
            if callable(post_shutdown):
                await post_shutdown(application)
        except Exception:
            logger.exception("webhook_post_shutdown_failed")

        try:
            await application.stop()
        except Exception:
            logger.exception("webhook_application_stop_failed")

        try:
            await application.shutdown()
        except Exception:
            logger.exception("webhook_application_shutdown_failed")
